"""Пере-верификация записанных отельных находок прижатой пробой шлюза."""
import asyncio, json, sqlite3
from pegasgap.cli import _load_dotenv
_load_dotenv()
from pegasgap.models import HotelDiagnosis, SearchParams
from pegasgap.providers.sletat_api import probe_hotels_with_tours

PROBEABLE = tuple(d.value for d in (
    HotelDiagnosis.LINKED_NO_OFFER, HotelDiagnosis.NOT_LINKED,
    HotelDiagnosis.CATALOG_DISABLED, HotelDiagnosis.IN_CATALOG_UNCHECKED))

async def main():
    conn = sqlite3.connect("pegasgap.db", timeout=120); conn.row_factory = sqlite3.Row
    runs = list(conn.execute(
        f"""select r.id, r.params_json, r.notes from runs r where exists (
              select 1 from gaps g where g.run_id = r.id and g.kind='hotel'
              and g.catalog_id is not null
              and g.diagnosis in ({','.join('?' * len(PROBEABLE))}))
            and r.notes not like '%пробой шлюза%'""", PROBEABLE))
    print(f"прогонов к пере-верификации: {len(runs)}", flush=True)
    total_drop = total_keep = 0
    for run in runs:
        params = SearchParams.model_validate_json(run["params_json"])
        rows = list(conn.execute(
            f"select id, catalog_id, hotel_name from gaps where run_id=? and kind='hotel' "
            f"and catalog_id is not null and diagnosis in ({','.join('?' * len(PROBEABLE))})",
            (run["id"], *PROBEABLE)))
        ids = sorted({r["catalog_id"] for r in rows})
        found = await probe_hotels_with_tours(params, ids)
        if found is None:
            print(f"  #{run['id']}: проба не состоялась — пропуск", flush=True)
            continue
        doomed = [r for r in rows if r["catalog_id"] in found]
        for r in doomed:
            conn.execute("delete from gaps where id=?", (r["id"],))
        notes = json.loads(run["notes"])
        if doomed:
            notes.append(f"снято отельных находок задним числом: {len(doomed)} из {len(rows)} — "
                         f"прижатый поиск шлюза НАШЁЛ туры (например {doomed[0]['hotel_name']})")
        else:
            notes.append(f"отельные находки ({len(rows)}) подтверждены прижатой пробой шлюза задним числом")
        conn.execute("update runs set notes=? where id=?",
                     (json.dumps(notes, ensure_ascii=False), run["id"]))
        conn.commit()
        total_drop += len(doomed); total_keep += len(rows) - len(doomed)
        print(f"  #{run['id']}: снято {len(doomed)} из {len(rows)}", flush=True)
    print(f"ИТОГО: снято {total_drop}, подтверждено {total_keep}")

asyncio.run(main())
