#!/usr/bin/env python3
import json
import argparse
import os
import sys

#  CAUTION: You need to have deployed the same number of organizations and msp in the fabric network before this works.
# ./generate_collections_config.py --msps Org1MSP Org2MSP Org3MSP \
#     --admin AdminMSP \
#     --include-admin-global \
#     --output collections_config.json
SCRIPT_DIR = os.path.abspath(os.path.dirname(__file__))
DEFAULT_OUTPUT = os.path.join(SCRIPT_DIR, "collections_config.json")

def make_policy(msp_list):
    # returns a string like "OR('Org1MSP.member','Org2MSP.member',…)"
    terms = [f"'{msp}.member'" for msp in msp_list]
    return "OR(" + ",".join(terms) + ")"

def main():
    parser = argparse.ArgumentParser(
        description="Generate a collections_config.json for Hyperledger Fabric private data."
    )
    parser.add_argument(
        "--msps", "-m",
        nargs="+", required=True,
        help="List of client MSP IDs (e.g. Org1MSP Org2MSP …)"
    )
    parser.add_argument(
        "--admin", "-a",
        default="AdminMSP",
        help="Name of your Admin MSP (default: AdminMSP)"
    )
    parser.add_argument(
        "--include-admin-global", "-g",
        action="store_true",
        help="Include the Admin MSP in the globalModelHashCollection policy"
    )
    parser.add_argument(
        "--output", "-o",
        default=DEFAULT_OUTPUT,
        help="Output filename (default: fsl-chaincode/collections_config.json)"
    )

    args = parser.parse_args()

    collections = []
    
    # Per-MSP collections
    for msp in args.msps:
        for prefix in ("clientModelHashCollection", "intermediateDataHashCollection"):
            collections.append({
                "name": f"{prefix}{msp}",
                "policy": make_policy([msp, args.admin]),
                "requiredPeerCount": 0,
                "maxPeerCount": 3,
                "blockToLive": 1000000,
                "memberOnlyRead": True,
                "memberOnlyWrite": True
            })

    # Global collection
    global_msps = list(args.msps)
    if args.include_admin_global:
        global_msps.append(args.admin)
    collections.append({
        "name": "globalModelHashCollection",
        "policy": make_policy(global_msps),
        "requiredPeerCount": 0,
        "maxPeerCount": 3,
        "blockToLive": 1000000,
        "memberOnlyRead": True,
        "memberOnlyWrite": True
    })

    # Write JSON
    try:
        with open(args.output, "w") as f:
            json.dump(collections, f, indent=2)
        print(f"✅ Wrote {len(collections)} collections to {args.output}")
    except Exception as e:
        print(f"❌ Failed to write {args.output}: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
