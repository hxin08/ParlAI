#!/usr/bin/env python3

# Copyright (c) 2017-present, Facebook, Inc.
# All rights reserved.
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree. An additional grant
# of patent rights can be found in the PATENTS file in the same directory.


"""
Useful utilities for training in distributed mode.
"""

import builtins
import pickle
try:
    import torch.version
    import torch.distributed as dist
    TORCH_AVAILABLE = True
except ImportError:
    TORCH_AVAILABLE = False


def validate_params(opt):
    """
    Ensure sane combinations of command line parameters for distributed training.

    Raises exceptions if anything is wrong, otherwise returns None.
    """
    if torch.version.__version__.startswith('0.'):
        raise ImportError(
            "Please upgrade to PyTorch >=1.0; "
            "visit https://pytorch.org for instructions."
        )

    if opt.get('no_cuda', False):
        raise ValueError(
            'Distributed mode only makes sense when using GPUs.'
        )

    if opt.get('numthreads', 1) != 1:
        raise ValueError(
            '--numthreads must be 1 for distributed training.'
        )

    if 'train:stream' in opt['datatype'] or 'ordered' in opt['datatype']:
        raise ValueError(
            "You should not combine ordered streaming with distributed training "
            "because all workers will have exactly the same minibatches, "
            "defeating the purpose."
        )


def is_distributed():
    """
    Returns True if we are in distributed mode.
    """
    return TORCH_AVAILABLE and dist.is_initialized()


def num_workers():
    """
    Get the total number of workers.
    """
    if not is_distributed():
        return 1
    else:
        return dist.get_world_size()


def is_primary_worker():
    """
    Returns False if we are a secondary worker. Returns True if we are either
    (1) not in distributed mode (2) or are the primary (rank 0) worker.
    """
    return not is_distributed() or dist.get_rank() == 0


def override_print(suppress=False, prefix=None):
    """
    Overrides the builtin print, to either mute or annotate the output of
    individual workers. Mutes if suppress is True, and this is not the
    primary or only worker.
    """
    builtin_print = builtins.print

    def new_print(*args, **kwargs):
        if suppress:
            # do nothing
            return
        elif prefix:
            return builtin_print(prefix, *args, **kwargs)
        else:
            # default to normal print
            return builtin_print(*args, **kwargs)

    builtins.print = new_print


def all_gather_list(data, group=None, max_size=16384):
    """Gathers arbitrary data from all nodes into a list.
    Similar to :func:`~torch.distributed.all_gather` but for arbitrary Python
    data. Note that *data* must be picklable.
    Args:
        data (Any): data from the local worker to be gathered on other workers
        group (optional): group of the collective
        max_size (int, optional): maximum size of the data to be gathered
            across workers
    """

    if not is_distributed():
        # fall back to just keeping things basic if we're not distributed
        return [data]

    # stolen shamelessly from fairseq
    # https://github.com/pytorch/fairseq/blob/c37250ab1c845919af721cd3f5c4cec2993aefe1/fairseq/distributed_utils.py#L116-L170
    rank = dist.get_rank()
    world_size = dist.get_world_size()

    buffer_size = max_size * world_size
    if (
        not hasattr(all_gather_list, '_buffer') or
        all_gather_list._buffer.numel() < buffer_size
    ):
        all_gather_list._buffer = torch.cuda.ByteTensor(buffer_size)

    buffer = all_gather_list._buffer
    buffer.zero_()

    enc = pickle.dumps(data)
    enc_size = len(enc)
    if enc_size + 2 > max_size:
        raise ValueError('encoded data exceeds max_size: {}'.format(enc_size + 2))
    assert max_size < 255 * 256

    buffer_rank = buffer[rank * max_size: (rank + 1) * max_size]
    buffer_rank[0] = enc_size // 255  # this encoding works for max_size < 65k
    buffer_rank[1] = enc_size % 255
    buffer_rank[2: enc_size + 2] = torch.ByteTensor(list(enc))

    dist.all_reduce(buffer, group=group)

    result = []
    for i in range(world_size):
        out_buffer = buffer[i * max_size: (i + 1) * max_size]
        size = (255 * out_buffer[0].item()) + out_buffer[1].item()
        if size > 0:
            result.append(
                pickle.loads(bytes(out_buffer[2:size+2].tolist()))
            )
    return result


def sync_object(data, max_size=16384):
    """
    Syncs an object among all workers, overriding everyone's version with the
    primary worker's. Data must be pickleable.
    """
    if not is_distributed():
        return data

    # prepare the buffer
    if (
        not hasattr(all_gather_list, '_buffer') or
        sync_object._buffer.numel() < max_size
    ):
        # cuda is safe because distributed mode is only okay with CUDA
        sync_object._buffer = torch.cuda.ByteTensor(max_size)

    buffer = sync_object._buffer

    if is_primary_worker():
        enc = pickle.dumps(data)
        enc_size = len(enc)
        if enc_size + 2 > max_size:
            raise ValueError('encoded data exceeds max_size')
        if enc_size > 255 * 255:
            # can't store the size in the first 2 bytes
            raise ValueError('encoded data exceeds max_size')

        buffer[0] = enc_size // 255
        buffer[1] = enc_size % 255
        buffer[2: enc_size + 2] = torch.ByteTensor(list(enc))

    dist.broadcast(buffer, 0)

    if not is_primary_worker():
        # deserialize the data
        enc_size = buffer[0] * 255 + buffer[1]
        data = pickle.loads(bytes(buffer[2: enc_size + 2].tolist()))

    return data


def sync_sum(number):
    """
    Compute the sum of a single number across all workers.
    """
    if not is_distributed():
        return number

    buffer = torch.cuda.FloatTensor([number])
    dist.all_reduce(buffer)
    # cast to the original type
    return type(number)(buffer.item())
