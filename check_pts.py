import asyncio, json

async def check():
    from services.pts_client import pts_graphql_query
    query = """
    { workOrderByID(id: "6a61c8166fd997384e4c2098") { id info { id note file { id filename originalName url } } } }
    """
    result = await pts_graphql_query(query)
    wo = result.get("workOrderByID", {})
    info_list = wo.get("info", [])
    print(f"infoList count: {len(info_list)}")
    for info in info_list:
        files = info.get("file", [])
        note = info.get("note", "")
        print(f"  note: {note[:80]}")
        print(f"  files: {len(files)}")
        for f in files:
            name = f.get("originalName") or f.get("filename") or ""
            url = str(f.get("url", ""))[:80]
            print(f"    name={name} url={url}")

asyncio.run(check())
