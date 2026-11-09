"""Shared bank traversal; mapper implementations supply the bus operation."""


def read_banks(read_bank, total_size, bank_size, progress=None):
    if (type(total_size) is not int or type(bank_size) is not int
            or not 0 <= total_size <= 64 * 1024 * 1024 or bank_size <= 0):
        raise ValueError('Invalid bank geometry')
    parts = []
    done = 0
    while done < total_size:
        size = min(bank_size, total_size - done)
        data = read_bank(done // bank_size, size)
        if not isinstance(data, bytes) or len(data) != size:
            raise ValueError('Bank reader returned an incomplete bank')
        parts.append(data)
        done += size
        if progress:
            progress(done, total_size)
    return b''.join(parts)
