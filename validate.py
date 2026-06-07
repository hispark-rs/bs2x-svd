"""Validate WS63.svd against CMSIS-SVD schema."""
import sys, urllib.request, os
from pathlib import Path
import xmlschema

SVD = Path(__file__).parent / "WS63.svd"
XSD_URL = "https://raw.githubusercontent.com/ARM-software/CMSIS_5/develop/CMSIS/Utilities/CMSIS-SVD.xsd"
XSD_CACHE = Path("/tmp/CMSIS-SVD.xsd")


def main() -> int:
    if not XSD_CACHE.exists():
        print(f"Downloading schema from {XSD_URL}...")
        urllib.request.urlretrieve(XSD_URL, XSD_CACHE)

    print(f"Validating {SVD} against CMSIS-SVD schema...")
    schema = xmlschema.XMLSchema(str(XSD_CACHE))

    try:
        schema.validate(str(SVD))
        print("PASS: SVD is valid against CMSIS-SVD schema.")
        return 0
    except Exception as e:
        print(f"FAIL: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
