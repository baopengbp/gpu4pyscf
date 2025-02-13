# Copyright 2023 The GPU4PySCF Authors. All Rights Reserved.
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

import numpy as np
import cupy
from cupy._environment import _preload_libs
from cupyx import cutensor
from cupy_backends.cuda.libs import cutensor as cutensor_backend
from cupy_backends.cuda.libs.cutensor import Handle

libcutensor = None
for lib_path in _preload_libs['cutensor']:
    try:
        libcutensor = _preload_libs['cutensor'][lib_path]
        break
    except Exception:
        continue

_handle = Handle()
_modes = {}
_contraction_descriptors = {}

cutensor_backend.init(_handle)

def _create_mode_with_cache(mode):
    integer_mode = []
    for x in mode:
        if isinstance(x, int):
            integer_mode.append(x)
        elif isinstance(x, str):
            integer_mode.append(ord(x))
        else:
            raise TypeError('Cannot create tensor mode: {}'.format(type(x)))
    key = tuple(integer_mode)

    if key in _modes:
        mode = _modes[key]
    else:
        mode = cutensor.create_mode(*mode)
        _modes[key] = mode
    return mode

def create_contraction_descriptor(handle,
                                  a, desc_a, mode_a,
                                  b, desc_b, mode_b,
                                  c, desc_c, mode_c):
    alignment_req_A = cutensor_backend.getAlignmentRequirement(handle, a.data.ptr, desc_a)
    alignment_req_B = cutensor_backend.getAlignmentRequirement(handle, b.data.ptr, desc_b)
    alignment_req_C = cutensor_backend.getAlignmentRequirement(handle, c.data.ptr, desc_c)

    key = (handle.ptr, cutensor_backend.COMPUTE_64F,
           desc_a.ptr, mode_a.data, alignment_req_A,
           desc_b.ptr, mode_b.data, alignment_req_B,
           desc_c.ptr, mode_c.data, alignment_req_C)

    if key in _contraction_descriptors:
        desc = _contraction_descriptors[key]
        return desc

    desc = cutensor_backend.ContractionDescriptor()
    cutensor_backend.initContractionDescriptor(
        handle,
        desc,
        desc_a, mode_a.data, alignment_req_A,
        desc_b, mode_b.data, alignment_req_B,
        desc_c, mode_c.data, alignment_req_C,
        desc_c, mode_c.data, alignment_req_C,
        cutensor_backend.COMPUTE_64F)
    _contraction_descriptors[key] = desc
    return desc

def create_contraction_find(handle, algo=cutensor_backend.ALGO_DEFAULT):
    find = cutensor_backend.ContractionFind()
    cutensor_backend.initContractionFind(handle, find, algo)
    return find

def contraction(pattern, a, b, alpha, beta, out=None):
    pattern = pattern.replace(" ", "")
    str_a, rest = pattern.split(',')
    str_b, str_c = rest.split('->')
    key = str_a + str_b
    val = list(a.shape) + list(b.shape)
    shape = {k:v for k, v in zip(key, val)}

    mode_a = list(str_a)
    mode_b = list(str_b)
    mode_c = list(str_c)

    if(out is not None):
        c = out
    else:
        c = cupy.empty([shape[k] for k in str_c], order='C')

    desc_a = cutensor.create_tensor_descriptor(a)
    desc_b = cutensor.create_tensor_descriptor(b)
    desc_c = cutensor.create_tensor_descriptor(c)

    mode_a = _create_mode_with_cache(mode_a)
    mode_b = _create_mode_with_cache(mode_b)
    mode_c = _create_mode_with_cache(mode_c)

    out = c
    desc = create_contraction_descriptor(_handle, a, desc_a, mode_a, b, desc_b, mode_b, c, desc_c, mode_c)
    find = create_contraction_find(_handle)
    ws_size = cutensor_backend.contractionGetWorkspaceSize(_handle, desc, find, cutensor_backend.WORKSPACE_RECOMMENDED)
    try:
        ws = cupy.empty(ws_size, dtype=np.int8)
    except Exception:
        ws_size = cutensor_backend.contractionGetWorkspaceSize(_handle, desc, find, cutensor_backend.WORKSPACE_MIN)
        ws = cupy.empty(ws_size, dtype=np.int8)

    plan = cutensor_backend.ContractionPlan()
    cutensor_backend.initContractionPlan(_handle, plan, desc, find, ws_size)
    alpha = np.asarray(alpha)
    beta = np.asarray(beta)
    cutensor_backend.contraction(_handle, plan,
                             alpha.ctypes.data, a.data.ptr, b.data.ptr,
                             beta.ctypes.data, c.data.ptr, out.data.ptr,
                             ws.data.ptr, ws_size)
    return out

import os
if 'CONTRACT_ENGINE' in os.environ:
    contract_engine = os.environ['CONTRACT_ENGINE']
else:
    contract_engine = None

if libcutensor is None:
    contract_engine = 'cupy'

# override the 'contract' function if einsum is customized or cutensor is not found
if contract_engine is not None:
    einsum = None
    if contract_engine == 'opt_einsum':
        import opt_einsum
        einsum = opt_einsum.contract
    elif contract_engine == 'cuquantum':
        from cuquantum import contract as einsum
    elif contract_engine == 'cupy':
        einsum = cupy.einsum
    else:
        raise RuntimeError('unknown tensor contraction engine.')

    import warnings
    warnings.warn(f'using {contract_engine} as the tensor contraction engine.')
    def contract(pattern, a, b, alpha=1.0, beta=0.0, out=None):
        if out is None:
            return cupy.asarray(einsum(pattern, a, b), order='C')
        else:
            out[:] = alpha*einsum(pattern, a, b) + beta*out
            return cupy.asarray(out, order='C')
else:
    def contract(pattern, a, b, alpha=1.0, beta=0.0, out=None):
        '''
        a wrapper for general tensor contraction
        pattern has to be a standard einsum notation
        '''
        return contraction(pattern, a, b, alpha, beta, out=out)
