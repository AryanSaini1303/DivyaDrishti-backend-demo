import asyncio
from db import execute_readonly_sql, close_pool


async def run():
    print("-- valid select --")
    print(await execute_readonly_sql("select id, name from franchises"))

    print("-- write attempt: update --")
    print(await execute_readonly_sql("update franchises set name = 'x' where id = gen_random_uuid()"))

    print("-- write attempt: drop --")
    print(await execute_readonly_sql("drop table franchises"))

    print("-- missing limit gets one added --")
    print(await execute_readonly_sql("select id from products"))

    print("-- excluded table via RLS/grant should fail, not silently return rows --")
    print(await execute_readonly_sql("select * from users"))

    print("-- excluded table entirely (no grant at all) --")
    print(await execute_readonly_sql("select * from customer_addresses"))

    print("-- statement timeout: forces a 6s sleep against a 5s cap --")
    print(await execute_readonly_sql("select pg_sleep(6)"))

    print("-- sql injection style stacked statement --")
    print(await execute_readonly_sql("select 1; drop table franchises;"))

    await close_pool()


if __name__ == "__main__":
    asyncio.run(run())