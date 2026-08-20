import asyncio, json, sqlite3
from datetime import date
from pegasgap.diagnosis import diagnose_reverse, reverse_index
from pegasgap.models import GapKind, HotelGap, ScanResult, SearchParams
from pegasgap.providers.tourvisor_api import fetch_country_hotels

async def main():
    conn = sqlite3.connect("pegasgap.db", timeout=120); conn.row_factory = sqlite3.Row
    runs = list(conn.execute(
        "select id, params_json from runs where id in (select distinct run_id from gaps where kind='reverse')"))
    dicts, updated = {}, 0
    for run in runs:
        params = json.loads(run["params_json"])
        country = params["destination_country"]
        if country not in dicts:
            dicts[country] = reverse_index(await fetch_country_hotels(country))
        rows = list(conn.execute(
            "select id, hotel_name, stars from gaps where run_id=? and kind='reverse'", (run["id"],)))
        gaps = [HotelGap(kind=GapKind.REVERSE, hotel_name=r["hotel_name"], stars=r["stars"]) for r in rows]
        scan = ScanResult(params=SearchParams(
            departure_city=params["departure_city"], destination_country=country,
            date_from=date.fromisoformat(params["date_from"]),
            date_to=date.fromisoformat(params["date_to"]),
            nights_min=params["nights_min"], nights_max=params["nights_max"],
            adults=params["adults"]), operator="", gaps=gaps)
        diagnose_reverse(scan, dicts[country])
        for row, gap in zip(rows, gaps):
            conn.execute("update gaps set diagnosis=?, note=?, reference_hotel_id=? where id=?",
                         (gap.diagnosis.value, gap.note, gap.reference_hotel_id, row["id"]))
            updated += 1
        conn.commit()
        print(f"прогон {run['id']}: {len(rows)} строк", flush=True)
    print(f"итого пересчитано: {updated}")

asyncio.run(main())
