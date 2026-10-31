"""Fail image builds if OSS CRC64 uses the slow Python fallback or is incorrect.

No network access or credentials are needed. Also usable as an image smoke test.
"""

import importlib

from oss2.utils import Crc64


def main() -> None:
    native = importlib.import_module("crcmod._crcfunext")
    crcmod_impl = importlib.import_module("crcmod.crcmod")
    if not crcmod_impl._usingExtension:
        raise RuntimeError("crcmod is not using its native C extension")

    # Fixed OSS CRC64 vector, verified against both native and Python backends.
    payload = bytes(range(256)) * 16384
    expected = 8805802654381464591
    whole = Crc64()
    whole(payload)
    streamed = Crc64()
    for offset in range(0, len(payload), 8193):
        streamed(payload[offset : offset + 8193])
    if whole.crc != expected or streamed.crc != expected:
        raise RuntimeError("OSS CRC64 checksum verification failed")
    print(f"Native OSS CRC64 verified: {native.__file__}")


if __name__ == "__main__":
    main()
