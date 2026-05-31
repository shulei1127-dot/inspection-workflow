"""Helper script: discover AITable structure for inspection work orders.

Usage:
    cd ~/Downloads/inspection-workflow
    PYTHONPATH=. python3 scripts/discover_aitable.py

This script will:
1. Search for AITable bases matching "巡检"
2. Show base info with table list
3. Show table schema with field IDs
"""

import asyncio
import json
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import dingtalk_client


async def main():
    print("=== Searching AITable bases for '巡检' ===")
    bases = await dingtalk_client.search_bases("巡检")
    print(json.dumps(bases, ensure_ascii=False, indent=2))

    if bases:
        for base in bases[:3]:  # Limit to first 3 bases
            base_id = base.get("baseId") or base.get("id", "")
            base_name = base.get("name", "unknown")
            print(f"\n=== Base: {base_name} ({base_id}) ===")

            base_info = await dingtalk_client.get_base(base_id)
            print(json.dumps(base_info, ensure_ascii=False, indent=2))

            # Get tables
            tables = base_info.get("tables", []) if base_info else []
            for table in tables[:5]:
                table_id = table.get("tableId") or table.get("id", "")
                table_name = table.get("name", "unknown")
                print(f"\n=== Table: {table_name} ({table_id}) ===")

                table_info = await dingtalk_client.get_table(base_id, table_id)
                print(json.dumps(table_info, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
