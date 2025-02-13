# gpu4pyscf is a plugin to use Nvidia GPU in PySCF package
#
# Copyright (C) 2022 Qiming Sun
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import time
import copy
import ctypes
import contextlib
import numpy as np
import cupy
import scipy.linalg
from functools import reduce
from pyscf import gto
from pyscf import lib as pyscf_lib
from pyscf.lib import logger
from pyscf.scf import hf, jk, _vhf
from gpu4pyscf import lib
from gpu4pyscf.lib.cupy_helper import eigh, load_library, tag_array
from gpu4pyscf.scf import diis

LMAX_ON_GPU = 4
FREE_CUPY_CACHE = True
BINSIZE = 128   # TODO bug for 256
libgvhf = load_library('libgvhf')

def get_jk(mol, dm, hermi=1, vhfopt=None, with_j=True, with_k=True, omega=None,
           verbose=None):
    '''Compute J, K matrices with CPU-GPU hybrid algorithm
    '''
    cput0 = (logger.process_clock(), logger.perf_counter())
    log = logger.new_logger(mol, verbose)
    if hermi != 1:
        raise NotImplementedError('JK-builder only supports hermitian density matrix')
    if omega is None:
        omega = 0.0
    if vhfopt is None:
        vhfopt = _VHFOpt(mol, 'int2e').build()
    out_cupy = isinstance(dm, cupy.ndarray)
    if not isinstance(dm, cupy.ndarray):
        dm = cupy.asarray(dm)
    coeff = cupy.asarray(vhfopt.coeff)
    nao, nao0 = coeff.shape
    dm0 = dm
    dms = cupy.asarray(dm0.reshape(-1,nao0,nao0))
    dms = [cupy.einsum('pi,ij,qj->pq', coeff, x, coeff) for x in dms]
    if dm0.ndim == 2:
        dms = cupy.asarray(dms[0], order='C').reshape(1,nao,nao)
    else:
        dms = cupy.asarray(dms, order='C')
    n_dm = dms.shape[0]
    scripts = []
    vj = vk = None
    vj_ptr = vk_ptr = pyscf_lib.c_null_ptr()
    if with_j:
        vj = cupy.zeros(dms.shape).transpose(0, 2, 1)
        vj_ptr = ctypes.cast(vj.data.ptr, ctypes.c_void_p)
        scripts.append('ji->s2kl')
    if with_k:
        vk = cupy.zeros(dms.shape).transpose(0, 2, 1)
        vk_ptr = ctypes.cast(vk.data.ptr, ctypes.c_void_p)
        if hermi == 1:
            scripts.append('jk->s2il')
        else:
            scripts.append('jk->s1il')

    l_symb = pyscf_lib.param.ANGULAR
    log_qs = vhfopt.log_qs
    direct_scf_tol = vhfopt.direct_scf_tol
    ncptype = len(log_qs)
    cp_idx, cp_jdx = np.tril_indices(ncptype)
    l_ctr_shell_locs = vhfopt.l_ctr_offsets
    l_ctr_ao_locs = vhfopt.mol.ao_loc[l_ctr_shell_locs]
    dm_ctr_cond = np.max(
        [pyscf_lib.condense('absmax', x, l_ctr_ao_locs) for x in dms.get()], axis=0)

    dm_shl = cupy.zeros([l_ctr_shell_locs[-1], l_ctr_shell_locs[-1]])
    assert dms.flags.c_contiguous
    size_l = np.array([1,3,6,10,15,21,28])
    l_ctr = vhfopt.uniq_l_ctr[:,0]
    r = 0
    for i, li in enumerate(l_ctr):
        i0 = l_ctr_ao_locs[i]
        i1 = l_ctr_ao_locs[i+1]
        ni_shls = (i1-i0)//size_l[li]
        c = 0
        for j, lj in enumerate(l_ctr):
            j0 = l_ctr_ao_locs[j]
            j1 = l_ctr_ao_locs[j+1]
            nj_shls = (j1-j0)//size_l[lj]
            sub_dm = dms[0][i0:i1,j0:j1].reshape([ni_shls, size_l[li], nj_shls, size_l[lj]])
            dm_shl[r:r+ni_shls, c:c+nj_shls] = cupy.max(sub_dm, axis=[1,3])
            c += nj_shls
        r += ni_shls

    dm_shl = cupy.asarray(np.log(dm_shl))
    nshls = dm_shl.shape[0]
    if hermi != 1:
        dm_ctr_cond = (dm_ctr_cond + dm_ctr_cond.T) * .5
    fn = libgvhf.GINTbuild_jk
    for cp_ij_id, log_q_ij in enumerate(log_qs):
        cpi = cp_idx[cp_ij_id]
        cpj = cp_jdx[cp_ij_id]
        li = vhfopt.uniq_l_ctr[cpi,0]
        lj = vhfopt.uniq_l_ctr[cpj,0]
        if li > LMAX_ON_GPU or lj > LMAX_ON_GPU or log_q_ij.size == 0:
            continue

        for cp_kl_id, log_q_kl in enumerate(log_qs[:cp_ij_id+1]):
            cpk = cp_idx[cp_kl_id]
            cpl = cp_jdx[cp_kl_id]
            lk = vhfopt.uniq_l_ctr[cpk,0]
            ll = vhfopt.uniq_l_ctr[cpl,0]
            if lk > LMAX_ON_GPU or ll > LMAX_ON_GPU or log_q_kl.size == 0:
                continue

            # TODO: determine cutoff based on the relevant maximum value of dm blocks?
            sub_dm_cond = max(dm_ctr_cond[cpi,cpj], dm_ctr_cond[cpk,cpl],
                              dm_ctr_cond[cpi,cpk], dm_ctr_cond[cpj,cpk],
                              dm_ctr_cond[cpi,cpl], dm_ctr_cond[cpj,cpl])
            if sub_dm_cond < direct_scf_tol * 1e3:
                continue

            #log_cutoff = np.log(direct_scf_tol / sub_dm_cond)
            log_cutoff = np.log(direct_scf_tol)
            sub_dm_cond = np.log(sub_dm_cond)

            bins_locs_ij = vhfopt.bins[cp_ij_id]
            bins_locs_kl = vhfopt.bins[cp_kl_id]

            log_q_ij = cupy.asarray(log_q_ij, dtype=np.float64)
            log_q_kl = cupy.asarray(log_q_kl, dtype=np.float64)

            bins_floor_ij = vhfopt.bins_floor[cp_ij_id]
            bins_floor_kl = vhfopt.bins_floor[cp_kl_id]
            #if li + lj + lk + ll < 8:
            #    continue
            nbins_ij = len(bins_locs_ij) - 1
            nbins_kl = len(bins_locs_kl) - 1
            err = fn(vhfopt.bpcache, vj_ptr, vk_ptr,
                     ctypes.cast(dms.data.ptr, ctypes.c_void_p),
                     ctypes.c_int(nao), ctypes.c_int(n_dm),
                     bins_locs_ij.ctypes.data_as(ctypes.c_void_p),
                     bins_locs_kl.ctypes.data_as(ctypes.c_void_p),
                     bins_floor_ij.ctypes.data_as(ctypes.c_void_p),
                     bins_floor_kl.ctypes.data_as(ctypes.c_void_p),
                     ctypes.c_int(nbins_ij),
                     ctypes.c_int(nbins_kl),
                     ctypes.c_int(cp_ij_id),
                     ctypes.c_int(cp_kl_id),
                     ctypes.c_double(omega),
                     ctypes.c_double(log_cutoff),
                     ctypes.c_double(sub_dm_cond),
                     ctypes.cast(dm_shl.data.ptr, ctypes.c_void_p),
                     ctypes.c_int(nshls),
                     ctypes.cast(log_q_ij.data.ptr, ctypes.c_void_p),
                     ctypes.cast(log_q_kl.data.ptr, ctypes.c_void_p)
                     )
            if err != 0:
                detail = f'CUDA Error for ({l_symb[li]}{l_symb[lj]}|{l_symb[lk]}{l_symb[ll]})'
                raise RuntimeError(detail)
            #log.debug1('(%s%s|%s%s) on GPU %.3fs',
            #           l_symb[li], l_symb[lj], l_symb[lk], l_symb[ll],
            #           time.perf_counter() - t0)
            #print(li, lj, lk, ll, time.perf_counter() - t0)
            #exit()
    if with_j:
        vj_ao = []
        #vj = [cupy.einsum('pi,pq,qj->ij', coeff, x, coeff) for x in vj]
        for x in vj:
            x = cupy.einsum('pi,pq->iq', coeff, x)
            x = cupy.einsum('iq,qj->ij', x, coeff)
            vj_ao.append(2.0*(x + x.T))
        vj = vj_ao

    if with_k:
        vk_ao = []
        for x in vk:
            x = cupy.einsum('pi,pq->iq', coeff, x)
            x = cupy.einsum('iq,qj->ij', x, coeff)
            vk_ao.append(x + x.T)
        vk = vk_ao

    cput0 = log.timer_debug1('get_jk pass 1 on gpu', *cput0)
    h_shls = vhfopt.h_shls
    if h_shls:
        log.debug3('Integrals for %s functions on CPU', l_symb[LMAX_ON_GPU+1])
        pmol = vhfopt.mol
        shls_excludes = [0, h_shls[0]] * 4
        vs_h = _vhf.direct_mapdm('int2e_cart', 's8', scripts,
                                 dms.get(), 1, pmol._atm, pmol._bas, pmol._env,
                                 vhfopt=vhfopt, shls_excludes=shls_excludes)
        coeff = vhfopt.coeff
        idx, idy = np.tril_indices(nao, -1)
        if with_j and with_k:
            vj1 = vs_h[0].reshape(n_dm,nao,nao)
            vk1 = vs_h[1].reshape(n_dm,nao,nao)
        elif with_j:
            vj1 = vs_h[0].reshape(n_dm,nao,nao)
        else:
            vk1 = vs_h[0].reshape(n_dm,nao,nao)

        if with_j:
            vj1[:,idy,idx] = vj1[:,idx,idy]
            vj1 = cupy.asarray(vj1)
            for i, v in enumerate(vj1):
                vj[i] += coeff.T.dot(v).dot(coeff)
        if with_k:
            if hermi:
                vk1[:,idy,idx] = vk1[:,idx,idy]
            vk1 = cupy.asarray(vk1)
            for i, v in enumerate(vk1):
                vk[i] += coeff.T.dot(v).dot(coeff)
        cput0 = log.timer_debug1('get_jk pass 2 for l>4 basis on cpu', *cput0)

    if FREE_CUPY_CACHE:
        coeff = dms = None
        cupy.get_default_memory_pool().free_all_blocks()

    if dm0.ndim == 2:
        if with_j:
            vj = vj[0]
        if with_k:
            vk = vk[0]
    else:
        if with_j:
            vj = cupy.asarray(vj).reshape(dm0.shape)
        if with_k:
            vk = cupy.asarray(vk).reshape(dm0.shape)
    if out_cupy:
        return vj, vk
    else:
        if with_j:
            vj = vj.get()
        if with_k:
            vk = vk.get()
        return vj, vk

def _get_jk(mf, mol=None, dm=None, hermi=1, with_j=True, with_k=True,
            omega=None):
    if omega is not None:
        assert omega >= 0

    cput0 = (logger.process_clock(), logger.perf_counter())
    log = logger.new_logger(mf)
    log.debug3('apply get_jk on gpu')
    if omega is None:
        if hasattr(mf, '_opt_gpu'):
            vhfopt = mf._opt_gpu
        else:
            vhfopt = _VHFOpt(mol, getattr(mf.opt, '_intor', 'int2e'),
                            getattr(mf.opt, 'prescreen', 'CVHFnrs8_prescreen'),
                            getattr(mf.opt, '_qcondname', 'CVHFsetnr_direct_scf'),
                            getattr(mf.opt, '_dmcondname', 'CVHFsetnr_direct_scf_dm'))
            vhfopt.build(mf.direct_scf_tol)
            mf._opt_gpu = vhfopt
    else:
        if hasattr(mf, '_opt_gpu_omega'):
            vhfopt = mf._opt_gpu_omega
        else:
            with mol.with_range_coulomb(omega):
                vhfopt = _VHFOpt(mol, getattr(mf.opt, '_intor', 'int2e'),
                                getattr(mf.opt, 'prescreen', 'CVHFnrs8_prescreen'),
                                getattr(mf.opt, '_qcondname', 'CVHFsetnr_direct_scf'),
                                getattr(mf.opt, '_dmcondname', 'CVHFsetnr_direct_scf_dm'))
                vhfopt.build(mf.direct_scf_tol)
                mf._opt_gpu_omega = vhfopt

    vj, vk = get_jk(mol, dm, hermi, vhfopt, with_j, with_k, omega, verbose=log)
    log.timer('vj and vk on gpu', *cput0)
    return vj, vk

def make_rdm1(mf, mo_coeff=None, mo_occ=None, **kwargs):
    if mo_occ is None: mo_occ = mf.mo_occ
    if mo_coeff is None: mo_coeff = mf.mo_coeff
    is_occ = mo_occ > 0
    mocc = mo_coeff[:, is_occ]
    dm = cupy.dot(mocc*mo_occ[is_occ], mocc.conj().T)
    occ_coeff = mo_coeff[:, mo_occ>1.0]
    return tag_array(dm, occ_coeff=occ_coeff, mo_occ=mo_occ, mo_coeff=mo_coeff)

def get_occ(mf, mo_energy=None, mo_coeff=None):
    if mo_energy is None: mo_energy = mf.mo_energy
    e_idx = cupy.argsort(mo_energy)
    nmo = mo_energy.size
    mo_occ = cupy.zeros(nmo)
    nocc = mf.mol.nelectron // 2
    mo_occ[e_idx[:nocc]] = 2
    return mo_occ

def get_veff(mf, mol=None, dm=None, dm_last=None, vhf_last=None, hermi=1, vhfopt=None):
    if dm_last is None or not mf.direct_scf:
        vj, vk = mf.get_jk(mol, cupy.asarray(dm), hermi)
        return vj - vk * .5
    else:
        ddm = cupy.asarray(dm) - cupy.asarray(dm_last)
        vj, vk = mf.get_jk(mol, ddm, hermi)
        return vj - vk * .5 + cupy.asarray(vhf_last)

def get_grad(mo_coeff, mo_occ, fock_ao):
    occidx = mo_occ > 0
    viridx = ~occidx
    g = reduce(cupy.dot, (mo_coeff[:,viridx].conj().T, fock_ao,
                           mo_coeff[:,occidx])) * 2
    return g.ravel()

def damping(s, d, f, factor):
    dm_vir = cupy.eye(s.shape[0]) - cupy.dot(s, d)
    f0 = reduce(cupy.dot, (dm_vir, f, d, s))
    f0 = (f0+f0.conj().T) * (factor/(factor+1.))
    return f - f0

def level_shift(s, d, f, factor):
    dm_vir = s - reduce(cupy.dot, (s, d, s))
    return f + dm_vir * factor

def get_fock(mf, h1e=None, s1e=None, vhf=None, dm=None, cycle=-1, diis=None,
             diis_start_cycle=None, level_shift_factor=None, damp_factor=None):
    if h1e is None: h1e = mf.get_hcore()
    if vhf is None: vhf = mf.get_veff(mf.mol, dm)
    f = h1e + vhf
    if cycle < 0 and diis is None:  # Not inside the SCF iteration
        return f

    if diis_start_cycle is None:
        diis_start_cycle = mf.diis_start_cycle
    if level_shift_factor is None:
        level_shift_factor = mf.level_shift
    if damp_factor is None:
        damp_factor = mf.damp
    if s1e is None: s1e = mf.get_ovlp()
    if dm is None: dm = mf.make_rdm1()

    if 0 <= cycle < diis_start_cycle-1 and abs(damp_factor) > 1e-4:
        f = damping(s1e, dm*.5, f, damp_factor)
    if diis is not None and cycle >= diis_start_cycle:
        f = diis.update(s1e, dm, f, mf, h1e, vhf)
    if abs(level_shift_factor) > 1e-4:
        f = level_shift(s1e, dm*.5, f, level_shift_factor)
    return f

def energy_elec(self, dm=None, h1e=None, vhf=None):
    '''
    electronic energy
    '''
    if dm is None: dm = self.make_rdm1()
    if h1e is None: h1e = self.get_hcore()
    if vhf is None: vhf = self.get_veff(self.mol, dm)
    e1 = cupy.einsum('ij,ji->', h1e, dm).real
    e_coul = cupy.einsum('ij,ji->', vhf, dm).real * .5
    self.scf_summary['e1'] = e1
    self.scf_summary['e2'] = e_coul
    logger.debug(self, 'E1 = %s  E_coul = %s', e1, e_coul)
    return e1+e_coul, e_coul

def _kernel(mf, conv_tol=1e-10, conv_tol_grad=None,
           dump_chk=True, dm0=None, callback=None, conv_check=True, **kwargs):
    conv_tol = mf.conv_tol
    mol = mf.mol
    t0 = (logger.process_clock(), logger.perf_counter())
    verbose = mf.verbose
    log = logger.new_logger(mol, verbose)
    if(conv_tol_grad is None):
        conv_tol_grad = conv_tol**.5
        logger.info(mf, 'Set gradient conv threshold to %g', conv_tol_grad)

    if(dm0 is None):
        dm0 = mf.get_init_guess(mol)

    dm = cupy.asarray(dm0, order='C')
    if hasattr(dm0, 'mo_coeff') and hasattr(dm0, 'mo_occ'):
        mo_coeff = cupy.asarray(dm0.mo_coeff)
        mo_occ = cupy.asarray(dm0.mo_occ)
        occ_coeff = cupy.asarray(mo_coeff[:,mo_occ>0])
        dm = tag_array(dm, occ_coeff=occ_coeff, mo_occ=mo_occ, mo_coeff=mo_coeff)

    # use optimized workflow if possible
    if hasattr(mf, 'init_workflow'):
        mf.init_workflow(dm0=dm)
        h1e = mf.h1e
        s1e = mf.s1e
    else:
        h1e = cupy.asarray(mf.get_hcore(mol))
        s1e = cupy.asarray(mf.get_ovlp(mol))

    vhf = mf.get_veff(mol, dm)
    e_tot = mf.energy_tot(dm, h1e, vhf)
    logger.info(mf, 'init E= %.15g', e_tot)
    t1 = log.timer_debug1('total prep', *t0)
    scf_conv = False

    if isinstance(mf.diis, lib.diis.DIIS):
        mf_diis = mf.diis
    elif mf.diis:
        assert issubclass(mf.DIIS, lib.diis.DIIS)
        mf_diis = mf.DIIS(mf, mf.diis_file)
        mf_diis.space = mf.diis_space
        mf_diis.rollback = mf.diis_space_rollback
        fock = mf.get_fock(h1e, s1e, vhf, dm)
        _, mf_diis.Corth = mf.eig(fock, s1e)
    else:
        mf_diis = None

    t_beg = time.time()
    for cycle in range(mf.max_cycle):
        t0 = (logger.process_clock(), logger.perf_counter())
        dm_last = dm
        last_hf_e = e_tot

        f = mf.get_fock(h1e, s1e, vhf, dm, cycle, mf_diis)
        t1 = log.timer_debug1('DIIS', *t0)
        mo_energy, mo_coeff = mf.eig(f, s1e)
        t1 = log.timer_debug1('eig', *t1)
        mo_occ = mf.get_occ(mo_energy, mo_coeff)
        dm = mf.make_rdm1(mo_coeff, mo_occ)
        t1 = log.timer_debug1('dm', *t1)
        vhf = mf.get_veff(mol, dm, dm_last, vhf)
        t1 = log.timer_debug1('veff', *t1)
        e_tot = mf.energy_tot(dm, h1e, vhf)
        t1 = log.timer_debug1('energy', *t1)

        norm_ddm = cupy.linalg.norm(dm-dm_last)
        t1 = log.timer_debug1('total', *t0)
        logger.info(mf, 'cycle= %d E= %.15g  delta_E= %4.3g  |ddm|= %4.3g',
                    cycle+1, e_tot, e_tot-last_hf_e, norm_ddm)
        e_diff = abs(e_tot-last_hf_e)
        norm_gorb = cupy.linalg.norm(mf.get_grad(mo_coeff, mo_occ, f))
        if(e_diff < conv_tol and norm_gorb < conv_tol_grad):
            scf_conv = True
            break

    if(cycle == mf.max_cycle):
        logger.warn("SCF failed to converge")

    t_end = time.time()
    mf.scf_time = t_end - t_beg
    # for dispersion correction
    e_tot = e_tot.get()
    if(hasattr(mf, 'get_dispersion')):
        e_disp = mf.get_dispersion()
        mf.e_disp = e_disp
        mf.e_mf = e_tot
        e_tot += e_disp

    return scf_conv, e_tot, mo_energy, mo_coeff, mo_occ

# tempory implemention, will be replaced after pyscf 2.2.0
def _gen_rhf_response(mf, mo_coeff=None, mo_occ=None,
                      singlet=None, hermi=0, max_memory=None):
    '''Generate a function to compute the product of RHF response function and
    RHF density matrices.

    Kwargs:
        singlet (None or boolean) : If singlet is None, response function for
            orbital hessian or CPHF will be generated. If singlet is boolean,
            it is used in TDDFT response kernel.
    '''

    if mo_coeff is None: mo_coeff = mf.mo_coeff
    if mo_occ is None: mo_occ = mf.mo_occ
    mol = mf.mol
    if isinstance(mf, hf.KohnShamDFT):
        from pyscf.dft import numint
        ni = mf._numint
        ni.libxc.test_deriv_order(mf.xc, 2, raise_error=True)
        if getattr(mf, 'nlc', '') != '':
            logger.warn(mf, 'NLC functional found in DFT object.  Its second '
                        'deriviative is not available. Its contribution is '
                        'not included in the response function.')
        omega, alpha, hyb = ni.rsh_and_hybrid_coeff(mf.xc, mol.spin)
        hybrid = abs(hyb) > 1e-10

        # mf can be pbc.dft.RKS object with multigrid
        if (not hybrid and
            'MultiGridFFTDF' == getattr(mf, 'with_df', None).__class__.__name__):
            from pyscf.pbc.dft import multigrid
            dm0 = mf.make_rdm1(mo_coeff, mo_occ)
            return multigrid._gen_rhf_response(mf, dm0, singlet, hermi)

        if singlet is None:
            # for ground state orbital hessian
            rho0, vxc, fxc = ni.cache_xc_kernel(mol, mf.grids, mf.xc,
                                                mo_coeff, mo_occ, 0)
        else:
            rho0, vxc, fxc = ni.cache_xc_kernel(mol, mf.grids, mf.xc,
                                                [mo_coeff]*2, [mo_occ*.5]*2, spin=1)
        dm0 = None  #mf.make_rdm1(mo_coeff, mo_occ)

        if singlet is None:
            # Without specify singlet, used in ground state orbital hessian
            def vind(dm1):
                # The singlet hessian
                if hermi == 2:
                    v1 = cupy.zeros_like(dm1)
                else:
                    v1 = ni.nr_rks_fxc(mol, mf.grids, mf.xc, dm0, dm1, 0, hermi,
                                       rho0, vxc, fxc, max_memory=max_memory)
                if hybrid or abs(alpha) > 1e-10:
                    if hermi != 2:
                        vj, vk = mf.get_jk(mol, dm1, hermi=hermi)
                        vk *= hyb
                        if omega > 1e-10:  # For range separated Coulomb
                            vk += mf.get_k(mol, dm1, hermi, omega) * (alpha-hyb)
                        v1 += vj - .5 * vk
                    else:
                        v1 -= .5 * hyb * mf.get_k(mol, dm1, hermi=hermi)
                elif hermi != 2:
                    v1 += mf.get_j(mol, dm1, hermi=hermi)
                return v1
        else:
            raise NotImplementedError('only singlet response is supported!')

    else:  # HF
        if (singlet is None or singlet) and hermi != 2:
            def vind(dm1):
                vj, vk = mf.get_jk(mol, dm1, hermi=hermi)
                return vj - .5 * vk
        else:
            def vind(dm1):
                return -.5 * mf.get_k(mol, dm1, hermi=hermi)

    return vind

def _quad_moment(mf, mol=None, dm=None, unit='Debye-Ang'):
    from pyscf.data import nist
    if mol is None: mol = mf.mol
    if dm is None: dm = mf.make_rdm1()
    nao = mol.nao
    with mol.with_common_orig((0,0,0)):
        ao_quad = mol.intor_symmetric('int1e_rr').reshape(3,3,nao,nao)

    el_quad = np.einsum('xyij,ji->xy', ao_quad, dm).real

    # Nuclear contribution
    charges = mol.atom_charges()
    coords  = mol.atom_coords()
    nucl_quad = np.einsum('i,ix,iy->xy', charges, coords, coords)

    mol_quad = nucl_quad - el_quad

    if unit.upper() == 'DEBYE-ANG':
        mol_quad *= nist.AU2DEBYE * nist.BOHR
    return mol_quad

def _eigh(mf, h, s):
    return eigh(h, s)

class RHF(hf.RHF):
    from gpu4pyscf.lib.utils import to_cpu, to_gpu, device

    screen_tol = 1e-14
    DIIS = diis.SCF_DIIS
    get_jk = _get_jk
    #_eigh = staticmethod(_eigh)
    _eigh = _eigh
    make_rdm1 = make_rdm1
    energy_elec = energy_elec
    get_fock = get_fock
    get_occ = get_occ
    get_veff = get_veff
    get_grad = staticmethod(get_grad)
    gen_response = _gen_rhf_response
    quad_moment = _quad_moment

    def scf(self, dm0=None, **kwargs):
        cput0 = (logger.process_clock(), logger.perf_counter())

        self.dump_flags()
        self.build(self.mol)

        if self.max_cycle > 0 or self.mo_coeff is None:
            self.converged, self.e_tot, \
                    self.mo_energy, self.mo_coeff, self.mo_occ = \
                    _kernel(self, self.conv_tol, self.conv_tol_grad,
                           dm0=dm0, callback=self.callback,
                           conv_check=self.conv_check, **kwargs)
        else:
            # Avoid to update SCF orbitals in the non-SCF initialization
            # (issue #495).  But run regular SCF for initial guess if SCF was
            # not initialized.
            self.e_tot = _kernel(self, self.conv_tol, self.conv_tol_grad,
                                dm0=dm0, callback=self.callback,
                                conv_check=self.conv_check, **kwargs)[1]

        logger.timer(self, 'SCF', *cput0)
        self._finalize()
        return self.e_tot
    kernel = pyscf_lib.alias(scf, alias_name='kernel')

    def reset(self, mol=None):
        if mol is not None:
            self.mol = mol
        self._opt_gpu = None
        self._opt_gpu_omega = None
        self._eri = None
        return self

    def nuc_grad_method(self):
        from gpu4pyscf.grad import rhf
        return rhf.Gradients(self)

    def density_fit(self, auxbasis=None, with_df=None, only_dfj=False):
        import gpu4pyscf.df.df_jk
        return gpu4pyscf.df.df_jk.density_fit(self, auxbasis, with_df, only_dfj)

class _VHFOpt(_vhf.VHFOpt):
    from gpu4pyscf.lib.utils import to_cpu, to_gpu, device

    def __init__(self, mol, intor, prescreen='CVHFnoscreen',
                 qcondname='CVHFsetnr_direct_scf', dmcondname=None):
        self.mol, self.coeff = basis_seg_contraction(mol)
        self.coeff = cupy.asarray(self.coeff)
        # Note mol._bas will be sorted in .build() method. VHFOpt should be
        # initialized after mol._bas updated.
        self._intor = intor
        self._prescreen = prescreen
        self._qcondname = qcondname
        self._dmcondname = dmcondname

    def build(self, cutoff=1e-13, group_size=None, diag_block_with_triu=False):
        cput0 = (logger.process_clock(), logger.perf_counter())
        mol = self.mol
        # Sort basis according to angular momentum and contraction patterns so
        # as to group the basis functions to blocks in GPU kernel.
        l_ctrs = mol._bas[:,[gto.ANG_OF, gto.NPRIM_OF]]
        uniq_l_ctr, _, inv_idx, l_ctr_counts = np.unique(
            l_ctrs, return_index=True, return_inverse=True, return_counts=True, axis=0)

        # Limit the number of AOs in each group
        if group_size is not None:
            uniq_l_ctr, l_ctr_counts = _split_l_ctr_groups(
                uniq_l_ctr, l_ctr_counts, group_size)

        if mol.verbose >= logger.DEBUG:
            logger.debug1(mol, 'Number of shells for each [l, nctr] group')
            for l_ctr, n in zip(uniq_l_ctr, l_ctr_counts):
                logger.debug(mol, '    %s : %s', l_ctr, n)

        sorted_idx = np.argsort(inv_idx, kind='stable').astype(np.int32)
        # Sort contraction coefficients before updating self.mol
        ao_loc = mol.ao_loc_nr(cart=True)
        nao = ao_loc[-1]
        # Some addressing problems in GPU kernel code
        assert nao < 32768
        ao_idx = np.array_split(np.arange(nao), ao_loc[1:-1])
        ao_idx = np.hstack([ao_idx[i] for i in sorted_idx])
        self.coeff = self.coeff[ao_idx]
        # Sort basis inplace
        mol._bas = mol._bas[sorted_idx]

        # Initialize vhfopt after reordering mol._bas
        _vhf.VHFOpt.__init__(self, mol, self._intor, self._prescreen,
                             self._qcondname, self._dmcondname)
        self.direct_scf_tol = cutoff

        lmax = uniq_l_ctr[:,0].max()
        nbas_by_l = [l_ctr_counts[uniq_l_ctr[:,0]==l].sum() for l in range(lmax+1)]
        l_slices = np.append(0, np.cumsum(nbas_by_l))
        if lmax >= LMAX_ON_GPU:
            self.g_shls = l_slices[LMAX_ON_GPU:LMAX_ON_GPU+2].tolist()
        else:
            self.g_shls = []
        if lmax > LMAX_ON_GPU:
            self.h_shls = l_slices[LMAX_ON_GPU+1:].tolist()
        else:
            self.h_shls = []

        # TODO: is it more accurate to filter with overlap_cond (or exp_cond)?
        q_cond = self.get_q_cond()
        cput1 = logger.timer(mol, 'Initialize q_cond', *cput0)
        log_qs = []
        pair2bra = []
        pair2ket = []
        bins = []
        bins_floor = []
        l_ctr_offsets = np.append(0, np.cumsum(l_ctr_counts))
        for i, (p0, p1) in enumerate(zip(l_ctr_offsets[:-1], l_ctr_offsets[1:])):
            if uniq_l_ctr[i,0] > LMAX_ON_GPU:
                # no integrals with h functions should be evaluated on GPU
                continue

            for q0, q1 in zip(l_ctr_offsets[:i], l_ctr_offsets[1:i+1]):
                q_sub = q_cond[p0:p1,q0:q1]
                idx = np.argwhere(q_sub > cutoff)
                q_sub = q_sub[idx[:,0], idx[:,1]]
                log_q = np.log(q_sub)
                log_q[log_q > 0] = 0
                nbins = (len(log_q) + BINSIZE)//BINSIZE
                s_index, bin_floor = _make_s_index(log_q, nbins=nbins, cutoff=cutoff)

                ishs = idx[:,0]
                jshs = idx[:,1]
                idx = np.lexsort((ishs, jshs, s_index), axis=-1)
                ishs = ishs[idx]
                jshs = jshs[idx]
                s_index = s_index[idx]

                ishs += p0
                jshs += q0
                pair2bra.append(ishs)
                pair2ket.append(jshs)
                bins.append(_make_bins(s_index, nbins=nbins))
                bins_floor.append(bin_floor)
                log_qs.append(cupy.asarray(log_q[idx]))

            q_sub = q_cond[p0:p1,p0:p1]
            idx = np.argwhere(q_sub > cutoff)
            if not diag_block_with_triu:
                # Drop the shell pairs in the upper triangle for diagonal blocks
                mask = idx[:,0] >= idx[:,1]
                idx = idx[mask,:]

            q_sub = q_sub[idx[:,0], idx[:,1]]
            log_q = np.log(q_sub)
            log_q[log_q > 0] = 0
            nbins = (len(log_q) + BINSIZE)//BINSIZE
            s_index, bin_floor = _make_s_index(log_q, nbins=nbins, cutoff=cutoff)
            ishs = idx[:,0]
            jshs = idx[:,1]
            idx = np.lexsort((ishs, jshs, s_index), axis=-1)
            ishs = ishs[idx]
            jshs = jshs[idx]
            s_index = s_index[idx]

            ishs += p0
            jshs += p0
            pair2bra.append(ishs)
            pair2ket.append(jshs)
            bins.append(_make_bins(s_index, nbins=nbins))
            bins_floor.append(bin_floor)
            log_qs.append(cupy.asarray(log_q[idx]))

        # TODO
        self.pair2bra = pair2bra
        self.pair2ket = pair2ket
        self.uniq_l_ctr = uniq_l_ctr
        self.l_ctr_offsets = l_ctr_offsets
        self.bas_pair2shls = np.hstack(
            pair2bra + pair2ket).astype(np.int32).reshape(2,-1)

        self.bas_pairs_locs = np.append(
            0, np.cumsum([x.size for x in pair2bra])).astype(np.int32)
        self.bins = bins
        self.bins_floor = bins_floor
        self.log_qs = log_qs
        ao_loc = mol.ao_loc_nr(cart=True)
        ncptype = len(log_qs)
        self.bpcache = ctypes.POINTER(BasisProdCache)()
        if diag_block_with_triu:
            scale_shellpair_diag = 1.
        else:
            scale_shellpair_diag = 0.5
        libgvhf.GINTinit_basis_prod(
            ctypes.byref(self.bpcache), ctypes.c_double(scale_shellpair_diag),
            ao_loc.ctypes.data_as(ctypes.c_void_p),
            self.bas_pair2shls.ctypes.data_as(ctypes.c_void_p),
            self.bas_pairs_locs.ctypes.data_as(ctypes.c_void_p), ctypes.c_int(ncptype),
            mol._atm.ctypes.data_as(ctypes.c_void_p), ctypes.c_int(mol.natm),
            mol._bas.ctypes.data_as(ctypes.c_void_p), ctypes.c_int(mol.nbas),
            mol._env.ctypes.data_as(ctypes.c_void_p))
        logger.timer(mol, 'Initialize GPU cache', *cput1)
        return self

    def clear(self):
        _vhf.VHFOpt.__del__(self)
        libgvhf.GINTdel_basis_prod(ctypes.byref(self.bpcache))
        return self

    def __del__(self):
        try:
            self.clear()
        except AttributeError:
            pass

class BasisProdCache(ctypes.Structure):
    pass

def basis_seg_contraction(mol, allow_replica=False):
    '''transform generally contracted basis to segment contracted basis
    Kwargs:
        allow_replica:
            transform the generally contracted basis to replicated
            segment-contracted basis
    '''
    bas_templates = {}
    _bas = []
    _env = mol._env.copy()
    contr_coeff = []
    aoslices = mol.aoslice_by_atom()
    for ia, (ib0, ib1) in enumerate(aoslices[:,:2]):
        key = tuple(mol._bas[ib0:ib1,gto.PTR_EXP])
        if key in bas_templates:
            bas_of_ia, coeff = bas_templates[key]
            bas_of_ia = bas_of_ia.copy()
            bas_of_ia[:,gto.ATOM_OF] = ia
        else:
            # Generate the template for decontracted basis
            coeff = []
            bas_of_ia = []
            for shell in mol._bas[ib0:ib1]:
                l = shell[gto.ANG_OF]
                nf = (l + 1) * (l + 2) // 2
                nctr = shell[gto.NCTR_OF]
                if nctr == 1:
                    bas_of_ia.append(shell)
                    coeff.append(np.eye(nf))
                    continue
                # Only basis with nctr > 1 needs to be decontracted
                nprim = shell[gto.NPRIM_OF]
                pcoeff = shell[gto.PTR_COEFF]
                if allow_replica:
                    coeff.extend([np.eye(nf)] * nctr)
                    bs = np.repeat(shell[np.newaxis], nctr, axis=0)
                    bs[:,gto.NCTR_OF] = 1
                    bs[:,gto.PTR_COEFF] = np.arange(pcoeff, pcoeff+nprim*nctr, nprim)
                    bas_of_ia.append(bs)
                else:
                    pexp = shell[gto.PTR_EXP]
                    exps = _env[pexp:pexp+nprim]
                    norm = gto.gto_norm(l, exps)
                    # remove normalization from contraction coefficients
                    c = _env[pcoeff:pcoeff+nprim*nctr].reshape(nctr,nprim)
                    c = np.einsum('ip,p,ef->iepf', c, 1/norm, np.eye(nf))
                    coeff.append(c.reshape(nf*nctr, nf*nprim).T)

                    _env[pcoeff:pcoeff+nprim] = norm
                    bs = np.repeat(shell[np.newaxis], nprim, axis=0)
                    bs[:,gto.NPRIM_OF] = 1
                    bs[:,gto.NCTR_OF] = 1
                    bs[:,gto.PTR_EXP] = np.arange(pexp, pexp+nprim)
                    bs[:,gto.PTR_COEFF] = np.arange(pcoeff, pcoeff+nprim)
                    bas_of_ia.append(bs)

            bas_of_ia = np.vstack(bas_of_ia)
            bas_templates[key] = (bas_of_ia, coeff)

        _bas.append(bas_of_ia)
        contr_coeff.extend(coeff)

    pmol = copy.copy(mol)
    pmol.cart = True
    pmol._bas = np.asarray(np.vstack(_bas), dtype=np.int32)
    pmol._env = _env
    contr_coeff = scipy.linalg.block_diag(*contr_coeff)

    if not mol.cart:
        contr_coeff = contr_coeff.dot(mol.cart2sph_coeff())
    return pmol, contr_coeff

def _make_s_index_offsets(log_q, nbins=10, cutoff=1e-12):
    '''Divides the shell pairs to "nbins" collections down to "cutoff"'''
    scale = nbins / np.log(min(cutoff, .1))
    s_index = np.floor(scale * log_q).astype(np.int32)
    bins = np.bincount(s_index)
    if bins.size < nbins:
        bins = np.append(bins, np.zeros(nbins-bins.size, dtype=np.int32))
    else:
        bins = bins[:nbins]
    assert bins.max() < 65536 * 8
    return np.append(0, np.cumsum(bins)).astype(np.int32)

def _make_s_index(log_q, nbins=10, cutoff=1e-12):
    '''Divides the shell pairs to "nbins" collections down to "cutoff"'''
    scale = nbins / np.log(min(cutoff, .1))
    s_index = np.floor(scale * log_q).astype(np.int32)
    bins_floor = np.arange(nbins) / scale
    return s_index, bins_floor

def _make_bins(s_index, nbins=10):
    bins = np.bincount(s_index)
    if bins.size < nbins:
        bins = np.append(bins, np.zeros(nbins-bins.size, dtype=np.int32))
    else:
        bins = bins[:nbins]
    assert bins.max() < 65536 * 8
    return np.append(0, np.cumsum(bins)).astype(np.int32)

def _split_l_ctr_groups(uniq_l_ctr, l_ctr_counts, group_size):
    '''Splits l_ctr patterns into small groups with group_size the maximum
    number of AOs in each group
    '''
    l = uniq_l_ctr[:,0]
    nf = l * (l + 1) // 2
    _l_ctrs = []
    _l_ctr_counts = []
    for l_ctr, counts in zip(uniq_l_ctr, l_ctr_counts):
        l = l_ctr[0]
        nf = (l + 1) * (l + 2) // 2
        max_shells = max(group_size // nf, 2)
        if l > LMAX_ON_GPU or counts <= max_shells:
            _l_ctrs.append(l_ctr)
            _l_ctr_counts.append(counts)
            continue

        nsubs, rests = counts.__divmod__(max_shells)
        _l_ctrs.extend([l_ctr] * nsubs)
        _l_ctr_counts.extend([max_shells] * nsubs)
        if rests > 0:
            _l_ctrs.append(l_ctr)
            _l_ctr_counts.append(rests)
    uniq_l_ctr = np.vstack(_l_ctrs)
    l_ctr_counts = np.hstack(_l_ctr_counts)
    return uniq_l_ctr, l_ctr_counts
