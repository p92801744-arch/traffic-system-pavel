import os

import uvicorn

from app.servicekit import AlreadyRunning, SingleInstance, clear_owner, current_role, local_site, runtime_dir, write_owner
from app.settings import settings


def main() -> None:
    role = current_role()
    site = local_site()
    lock = None
    if role == "all":
        # Ручной запуск занимает тот же замок, что и автозапуск: двух копий не будет.
        try:
            lock = SingleInstance(runtime_dir() / "supervisor.lock")
        except AlreadyRunning:
            print(f"Ящик уже запущен. Открой {site}")
            print("Проверка: python -m app.supervisor status")
            return
        lock.write_pid()
        write_owner("manual")
    elif role == "web":
        try:
            lock = SingleInstance(runtime_dir() / "web.lock")
        except AlreadyRunning:
            print(f"Сайт уже запущен. Открой {site}")
            return
        lock.write_pid()
    try:
        uvicorn.run(
            "app.main:app",
            host=settings.host,
            port=settings.port,
            reload=False,
        )
    finally:
        if role == "all":
            clear_owner(os.getpid())
        if lock is not None:
            lock.close()


if __name__ == "__main__":
    main()
