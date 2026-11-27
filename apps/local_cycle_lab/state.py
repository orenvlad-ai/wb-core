"""Owned persistent lab state, exclusive writer and recoverable explicit reset."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

SCHEMA = "wbc-local-lab-v1"


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("x") as stream:
            os.chmod(temporary, 0o600)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("Ожидался обычный файл стенда: " + str(path))
    return json.loads(path.read_text())


def stamp():
    return datetime.now(timezone.utc).isoformat()


class LabState:
    def __init__(self, root):
        supplied = Path(root).absolute()
        if supplied.is_symlink() or supplied.resolve() != supplied:
            raise ValueError("Путь стенда должен быть абсолютным, без символических ссылок")
        self.root = supplied
        self.runtime = supplied / "runtime"
        self.manifest = supplied / "lab.json"
        self._writer_fd = None

    def open(self, scenario, variant):
        source = Path(scenario).read_bytes()
        digest = hashlib.sha256(source).hexdigest()
        fresh = not self.root.exists()
        if fresh:
            self.root.mkdir(parents=True, mode=0o700)
        else:
            # Refuse a foreign directory before creating even a lock file.
            self.identity()
        with self.writer():
            saved = self.root / "scenario.json"
            if fresh:
                saved.write_bytes(source)
                atomic_json(self.manifest, dict(schema=SCHEMA, id=uuid4().hex,
                    generation=uuid4().hex, fixture=digest, variant=variant, created_at=stamp()))
            data = self.identity()
            if data["fixture"] != digest or data["variant"] != variant or saved.read_bytes() != source:
                raise ValueError("Сценарий или вариант отличаются от сохранённого стенда")
            for name in ("home", "tmp", "control", "runs", "archives"):
                if name == "runs" and (self.root / "reset-transaction.json").exists():
                    continue  # Recovery distinguishes an unmoved directory from a new empty one.
                (self.root / name).mkdir(exist_ok=True, mode=0o700)
            return data

    def identity(self):
        data = read_json(self.manifest)
        if data.get("schema") != SCHEMA or not all(data.get(k) for k in ("id", "generation", "fixture", "variant")):
            raise ValueError("Папка не принадлежит локальному стенду WBC")
        for parent, directories, files in os.walk(self.root, followlinks=False):
            if any((Path(parent) / name).is_symlink() for name in directories + files):
                raise ValueError("Символические ссылки в хранилище стенда запрещены")
        return data

    @contextmanager
    def writer(self):
        fd = os.open(self.root / ".writer.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("Стенд уже открыт: второй writer или сброс запрещён") from exc
            self._writer_fd = fd
            yield fd
        finally:
            self._writer_fd = None
            os.close(fd)

    def reset(self, expected_id):
        self.require_writer()
        data = self.identity()
        if expected_id != data["id"]:
            raise ValueError("Для сброса нужен точный ID этого стенда")
        journal = self.root / "reset-transaction.json"
        if journal.exists():
            return self.recover_reset()
        destination = self.root / "archives" / data["generation"]
        if destination.exists():
            raise ValueError("Архив уже существует без журнала сброса; данные сохранены")
        after = {**data, "generation": uuid4().hex, "reset_at": stamp(), "previous_archive": str(destination)}
        atomic_json(journal, dict(schema="wbc-local-reset-v1", before=data, after=after))
        return self.recover_reset()

    def recover_reset(self):
        self.require_writer()
        journal = self.root / "reset-transaction.json"
        if not journal.exists():
            return None
        value = read_json(journal)
        if value.get("schema") != "wbc-local-reset-v1":
            raise ValueError("Неизвестный журнал сброса")
        before, after = value["before"], value["after"]
        current = self.identity()
        if current not in (before, after) or before["id"] != after["id"]:
            raise ValueError("Состояние изменилось во время сброса")
        destination = self.root / "archives" / before["generation"]
        if after.get("previous_archive") != str(destination) or destination.is_symlink():
            raise ValueError("Архив не соответствует журналу сброса")
        destination.mkdir(mode=0o700, exist_ok=True)
        archived = destination / "lab.json"
        if archived.exists() and read_json(archived) != before:
            raise ValueError("Архив принадлежит другому сбросу")
        atomic_json(archived, before)
        for name in ("runtime", "runs"):
            source, target = self.root / name, destination / name
            if current == before and source.exists():
                if target.exists():
                    raise ValueError("Одновременно найдены исходные и архивные данные: " + name)
                os.rename(source, target)
        atomic_json(self.manifest, after)
        # Recreate working paths only after the new generation is durable.
        (self.root / "runs").mkdir(mode=0o700, exist_ok=True)
        journal.unlink()
        return destination

    def require_writer(self):
        if self._writer_fd is None:
            raise ValueError("Сброс возможен только под эксклюзивной блокировкой стенда")

    def checkpoint(self, directory, db_path):
        """A coherent DB image; attachments remain in the persistent runtime."""
        directory = Path(directory)
        with sqlite3.connect(Path(db_path).as_uri() + "?mode=ro", uri=True) as source:
            source.execute("PRAGMA query_only=ON")
            with sqlite3.connect(directory / "state.sqlite3") as target:
                source.backup(target)
        payload = (directory / "state.sqlite3").read_bytes()
        return hashlib.sha256(payload).hexdigest()
