"""数据库基础（真实 SQLite 连接）。

- 声明基类 Base 供所有 ORM 实体继承（models/orm.py 从此导入）；
- get_engine 惰性创建引擎；SessionLocal 提供会话工厂；
- init_db 在应用启动时建表，并写入与前端默认小说对齐的示例作品，
  保证角色库等模块开箱即可访问；
- get_session 是 FastAPI 依赖，提供请求级数据库会话。
"""
import logging
from pathlib import Path
from sqlalchemy import create_engine, event, Engine
from sqlalchemy.orm import declarative_base, sessionmaker, Session

from app.core.config import DEFAULT_DB_URL

Base = declarative_base()

_engine = None
SessionLocal = None


logger = logging.getLogger(__name__)


@event.listens_for(Engine, "connect")
def _load_sqlite_vec(dbapi_conn, _record):
    """每个新 sqlite3 连接加载 sqlite-vec 扩展（vec0 虚拟表查询必需）。

    加载失败静默跳过——vector_store 会自动回退纯 Python 余弦，接口不变。
    """
    if type(dbapi_conn).__module__ != "sqlite3":
        return
    try:
        import sqlite_vec
        dbapi_conn.enable_load_extension(True)
        sqlite_vec.load(dbapi_conn)
        dbapi_conn.enable_load_extension(False)
    except Exception as e:  # noqa: BLE001
        # 加载失败不阻断（vector_store 自动回退纯 Python 余弦，接口不变），但要留痕：
        # 否则「检索一直很慢」会让人反复怀疑数据量，而真实原因是扩展没装上（Phase 3.5）。
        # 用 debug 级：每个新连接都会触发，warning 会刷屏；排查时 NA_LOG_LEVEL=DEBUG 可见。
        logger.debug(f"[database] sqlite-vec 扩展未加载（将回退纯 Python 余弦）: {type(e).__name__}: {e}")


@event.listens_for(Engine, "connect")
def _set_sqlite_concurrency_pragma(dbapi_conn, _record):
    """SQLite 并发与耐久性设置（2026-09-12，为**多 AI 并行灌数据**铺路）。

    - `journal_mode=WAL`：读不阻塞写、写不阻塞读。默认的 rollback journal 下，
      一个写事务会锁住整库，多个导入进程并行时几乎必然互锁。
    - `busy_timeout=5000`：写锁竞争时**等待 5 秒**再报错。默认 0 表示立刻抛
      `database is locked` —— 批量导入时每次提交都是一次写事务，没有这个必然是满地失败。
    - `synchronous=NORMAL`：WAL 下的推荐档位（崩溃可能丢最后一个事务，但不会损坏库）。
      这是导入批任务的合理取舍；要绝对 durability 才需要 FULL。

    加载失败只警告不阻断：这些是**性能/并发优化**，不是功能前提。
    """
    if type(dbapi_conn).__module__ != "sqlite3":
        return
    try:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[database] SQLite 并发 PRAGMA 设置失败（不影响功能，"
                       f"并行导入时可能遇到 database is locked）: {type(e).__name__}: {e}")


def get_engine():
    global _engine, SessionLocal
    if _engine is None:
        # SQLite 不会自动创建上层目录，需提前建好 data/，否则报 unable to open database file
        if DEFAULT_DB_URL.startswith("sqlite"):
            db_path = Path(DEFAULT_DB_URL.replace("sqlite:///", "", 1))
            db_path.parent.mkdir(parents=True, exist_ok=True)
        # SQLite 在多线程（FastAPI）下需要关闭同线程检查
        connect_args = (
            {"check_same_thread": False, "timeout": 30}  # timeout=多进程并发写的 busy_timeout（2026-09-18）
            if DEFAULT_DB_URL.startswith("sqlite")
            else {}
        )
        _engine = create_engine(DEFAULT_DB_URL, future=True, connect_args=connect_args)  # 多进程并发写：connect_args 含 timeout 30s（2026-09-18）
        SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)
    return _engine


def get_session():
    """FastAPI 依赖：请求级会话，自动关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _auto_migrate(engine):
    """SQLite 增量迁移：比对 ORM 定义与已有表结构，缺列则自动 ALTER TABLE ADD COLUMN。

    根治「新增 ORM 列后报 no such column」——SQLite 不支持 create_all 的增量变更，
    这里手动补齐缺失列，避免每次都要手工写 ALTER 或删库重建。
    仅处理「新增列」场景（脚手架阶段够用）；删除/重命名列不在范围内。
    """
    from sqlalchemy import inspect, text
    from sqlalchemy import String, Integer, Float, Boolean, DateTime, Text, JSON

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    # 确保 ORM 已注册
    import app.models.orm  # noqa: F401

    added = []
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # 全新表交给下面的 create_all 处理
        existing_cols = {c["name"] for c in inspector.get_columns(table.name)}
        for col in table.columns:
            if col.name in existing_cols:
                continue
            # 推导列类型（SQLAlchemy 方言无关名）
            py_type = col.type
            type_name = py_type.__class__.__name__.upper()
            # 统一映射为 SQLite 友好的类型名
            sqlite_type = {
                "STRING": "TEXT",
                "TEXT": "TEXT",
                "INTEGER": "INTEGER",
                "FLOAT": "REAL",
                "BOOLEAN": "INTEGER",
                "DATETIME": "TEXT",
                "JSON": "TEXT",
            }.get(type_name, "TEXT")
            nullable = "" if col.nullable else " NOT NULL"
            default = ""
            # 仅标量默认可用；函数型默认(如 datetime.utcnow) 用 Python 触发，无法内联到 DDL
            if col.default is not None and not callable(col.default.arg):
                dv = col.default.arg
                if isinstance(dv, bool):
                    default = f" DEFAULT {1 if dv else 0}"
                elif isinstance(dv, (int, float)):
                    default = f" DEFAULT {dv}"
                elif isinstance(dv, (str, list, dict)):
                    import json as _json
                    lit = _json.dumps(dv) if isinstance(dv, (list, dict)) else dv
                    default = f" DEFAULT '{lit}'"
            # 已有数据表上给非空列加默认值缺失会失败，稳妥起见：非空列又无默认值时放宽成可空
            if not col.nullable and not default:
                nullable = ""
            stmt = text(
                f'ALTER TABLE {table.name} ADD COLUMN {col.name} {sqlite_type}{nullable}{default}'
            )
            with engine.begin() as conn:
                conn.execute(stmt)
            added.append(f"{table.name}.{col.name} ({sqlite_type})")
    return added


def init_db():
    """建表 + 增量迁移。幂等，可重复调用。

    1) create_all 负责新建尚未存在的表；
    2) _auto_migrate 负责给已有表补齐 ORM 中新增的列（SQLite 不支持自动增量变更）；
    3) _ensure_vec_index 建 sqlite-vec 虚拟表（A 线检索升级的可选加速索引，
       失败不阻断启动——vector_store 会走纯 Python 回退）。
    """
    engine = get_engine()
    import app.models.orm  # noqa: F401
    Base.metadata.create_all(bind=engine)
    try:
        added = _auto_migrate(engine)
        if added:
            logger.info(f"[init_db] 自动迁移新增列: {', '.join(added)}")
    except Exception as e:  # 迁移失败不应阻断启动
        logger.warning(f"[init_db] 自动迁移跳过/失败: {e}")
    _ensure_vec_index(engine)


def _ensure_vec_index(engine):
    """建/升级 vec0 虚拟表（向量 KNN 加速索引）。失败静默——接口层自会回退。

    ⚠️ vec_index 是**全局表**（所有作品 + 全局资料池共用一张），KNN 必须靠
    project_id/source_type 辅助列在查询内先过滤再取 k——否则全局池会挤占
    top-k 名额（2026-09-10 实测踩中：4 份本项目资料只召回 1 份）。
    故 schema 必须含这两个辅助列；检测到旧 schema 直接删表重建（索引可重建，
    source of truth 在 vector_chunks），并从 vector_chunks 自愈回填。
    """
    from sqlalchemy import text

    try:
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='vec_index'"
            )).fetchone()
            if row is not None and "project_id" not in (row[0] or ""):
                logger.info("[init_db] vec_index 为旧 schema（缺 project_id 辅助列），删除重建")
                conn.execute(text("DROP TABLE vec_index"))
                conn.commit()
                row = None

            conn.execute(text(
                "CREATE VIRTUAL TABLE IF NOT EXISTS vec_index USING vec0("
                "chunk_id text primary key, embedding float[1024], "
                "project_id text, source_type text)"
            ))
            conn.commit()

            # 虚拟表是空的但主表有数据（首次建表 / 刚重建）→ 从 vector_chunks 回填
            n_vec = conn.execute(text("SELECT COUNT(*) FROM vec_index")).scalar() or 0
            if not n_vec:
                try:
                    n_src = conn.execute(text("SELECT COUNT(*) FROM vector_chunks")).scalar() or 0
                except Exception as e:  # noqa: BLE001
                    # 数不出主表行数 → 当作 0（跳过回填）。留痕，便于区分「确实没数据」与「查询失败」
                    logger.debug(f"[database] 统计 vector_chunks 行数失败，跳过回填: {type(e).__name__}: {e}")
                    n_src = 0
                if n_src:
                    from app.services.vector_store import rebuild_vec_index
                    from sqlalchemy.orm import Session
                    with Session(engine) as s:
                        n = rebuild_vec_index(s)
                        s.commit()
                    logger.info(f"[init_db] vec_index 已从 vector_chunks 回填 {n} 块")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[init_db] vec_index 虚拟表不可用（向量检索走暴力回退）: {type(e).__name__}: {e}")
