import configparser
import os
import sys
import time
import uuid
import posixpath
import tempfile
import threading
import webbrowser
from functools import wraps
from pathlib import Path

import pymysql
import paramiko
from sshtunnel import SSHTunnelForwarder
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    session,
    abort,
    send_from_directory,
)
from werkzeug.utils import secure_filename
from werkzeug.security import check_password_hash
from waitress import serve


def resource_path(relative_path: str) -> str:
    """
    兼容 PyInstaller 打包后的路径。
    """
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)


def app_dir() -> Path:
    """
    exe 模式：返回 exe 所在目录。
    源码模式：返回当前项目目录。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def load_config() -> configparser.ConfigParser:
    """
    配置读取顺序：
    1. 优先读取 exe 同目录的 config.ini，方便你自己覆盖配置
    2. 如果没有外部 config.ini，则读取打包进 exe 的 config.ini
    """
    external_config = app_dir() / "config.ini"

    config = configparser.ConfigParser()

    if external_config.exists():
        config.read(external_config, encoding="utf-8")
        return config

    bundled_config = resource_path("config.ini")

    if os.path.exists(bundled_config):
        config.read(bundled_config, encoding="utf-8")
        return config

    raise RuntimeError(
        f"找不到 config.ini。请确认 config.ini 在 exe 同目录，或者已经打包进 exe。当前查找路径：{external_config}"
    )


config = load_config()
ssh_tunnel = None
ssh_tunnel_lock = threading.Lock()

app = Flask(
    __name__,
    template_folder=resource_path("templates"),
    static_folder=resource_path("static"),
)

# Use a per-start secret so browser cookies from previous launches cannot
# authenticate this process.
app.secret_key = f"{config['app'].get('secret_key', 'change-this-secret')}:{uuid.uuid4().hex}"

active_clients = {}
active_clients_lock = threading.Lock()
saw_browser_client = False
last_browser_seen_at = 0.0
shutdown_started = False


def external_image_url(public_path: str) -> str:
    """
    Convert a database image path into a browser-visible URL for this admin app.
    The database keeps the frontend path, while the local admin may need a
    different origin or a local file proxy to preview images.
    """
    if not public_path:
        return ""

    if public_path.startswith(("http://", "https://", "//")):
        return public_path

    image_base_url = config.get("display", "image_base_url", fallback="").strip().rstrip("/")
    if image_base_url:
        return f"{image_base_url}/{public_path.lstrip('/')}"

    public_prefix = config["upload"].get("public_prefix", "").rstrip("/")
    if public_prefix and public_path.startswith(public_prefix):
        filename = public_path.replace(public_prefix, "", 1).lstrip("/")
        if filename and "/" not in filename and "\\" not in filename:
            return url_for("uploaded_file", filename=filename)

    return public_path


app.jinja_env.filters["image_url"] = external_image_url


UI_DEFAULTS = {
    "app_name": "House Admin",
    "dashboard_label": "仪表盘",
    "listing_name": "房源",
    "listing_nav": "房源管理",
    "listing_title_label": "标题",
    "listing_category_label": "分类",
    "listing_price_label": "价格",
    "listing_layout_label": "户型 / 布局",
    "listing_area_label": "面积",
    "listing_location_label": "位置",
    "listing_description_label": "详细描述",
    "listing_carousel_label": "显示在轮播图",
    "listing_carousel_column": "轮播",
    "listing_main_image_label": "主图",
    "listing_sub_images_label": "副图",
    "listing_gallery_label": "已有副图",
    "category_nav": "分类管理",
    "category_name_label": "分类名称",
    "settings_nav": "网站设置",
    "settings_site_title_label": "网站标题",
    "settings_latest_title_label": "最新发布标题",
    "settings_featured_title_label": "精选内容标题",
    "settings_enter_text_label": "进入按钮文本",
    "settings_view_all_text_label": "查看全部按钮文本",
    "settings_catalog_title_label": "目录标题",
    "login_title": "后台登录",
}


def ui_text(key: str) -> str:
    default = UI_DEFAULTS[key]
    return config.get("ui", key, fallback=default).strip() or default


@app.context_processor
def inject_ui_text():
    ui = {key: ui_text(key) for key in UI_DEFAULTS}
    return {"ui": ui}


def shutdown_delay_seconds() -> float:
    return config["app"].getfloat("shutdown_delay_seconds", 1.0)


def client_timeout_seconds() -> float:
    return config["app"].getfloat("client_timeout_seconds", 20.0)


def shutdown_grace_seconds() -> float:
    return config["app"].getfloat("shutdown_grace_seconds", 8.0)


def mark_client_seen(page_id: str):
    global saw_browser_client, last_browser_seen_at

    if not page_id:
        return

    now = time.time()
    with active_clients_lock:
        active_clients[page_id] = now
        saw_browser_client = True
        last_browser_seen_at = now


def mark_client_left(page_id: str):
    global last_browser_seen_at

    if not page_id:
        return

    with active_clients_lock:
        active_clients.pop(page_id, None)
        last_browser_seen_at = time.time()


def schedule_shutdown(delay=None):
    global shutdown_started

    with active_clients_lock:
        if shutdown_started:
            return
        shutdown_started = True

    wait_seconds = shutdown_delay_seconds() if delay is None else delay

    def stop_process():
        time.sleep(max(wait_seconds, 0))
        os._exit(0)

    threading.Thread(target=stop_process, daemon=True).start()


def monitor_browser_clients():
    while True:
        time.sleep(2)
        now = time.time()
        should_shutdown = False

        with active_clients_lock:
            stale_before = now - client_timeout_seconds()
            stale_ids = [
                page_id
                for page_id, seen_at in active_clients.items()
                if seen_at < stale_before
            ]
            for page_id in stale_ids:
                active_clients.pop(page_id, None)

            if (
                saw_browser_client
                and not active_clients
                and now - last_browser_seen_at >= shutdown_grace_seconds()
            ):
                should_shutdown = True

        if should_shutdown:
            schedule_shutdown()
            return


@app.route("/client/ping", methods=["POST"])
def client_ping():
    mark_client_seen(request.args.get("page_id", ""))
    return ("", 204)


@app.route("/client/leave", methods=["POST"])
def client_leave():
    mark_client_left(request.args.get("page_id", ""))
    return ("", 204)

# -----------------------------
# 数据库
# -----------------------------

def get_ssh_tunnel():
    global ssh_tunnel

    use_tunnel = config["database"].getboolean(
        "use_ssh_tunnel",
        fallback=False,
    )

    if not use_tunnel:
        return None

    with ssh_tunnel_lock:
        if ssh_tunnel and ssh_tunnel.is_active:
            return ssh_tunnel

        ssh_host = config["sftp"].get("host")
        ssh_port = config["sftp"].getint("port", 22)
        ssh_username = config["sftp"].get("username")
        ssh_password = config["sftp"].get("password", fallback=None)
        key_path = config["sftp"].get("key_path", fallback=None)

        tunnel_options = {
            "ssh_address_or_host": (ssh_host, ssh_port),
            "ssh_username": ssh_username,
            "remote_bind_address": (
                config["database"].get("host", "127.0.0.1"),
                config["database"].getint("port", 3306),
            ),
            "local_bind_address": ("127.0.0.1", 0),
        }

        if key_path:
            tunnel_options["ssh_pkey"] = os.path.expanduser(key_path)
        else:
            tunnel_options["ssh_password"] = ssh_password

        ssh_tunnel = SSHTunnelForwarder(**tunnel_options)
        ssh_tunnel.start()

        print(
            "SSH database tunnel started: "
            f"127.0.0.1:{ssh_tunnel.local_bind_port}"
        )

        return ssh_tunnel

def get_db():
    tunnel = get_ssh_tunnel()

    if tunnel:
        db_host = "127.0.0.1"
        db_port = tunnel.local_bind_port
    else:
        db_host = config["database"].get("host")
        db_port = config["database"].getint("port", 3306)

    return pymysql.connect(
        host=db_host,
        port=db_port,
        user=config["database"].get("user"),
        password=config["database"].get("password"),
        database=config["database"].get("database"),
        charset=config["database"].get("charset", "utf8mb4"),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
        connect_timeout=10,
    )


def fetch_one(sql, params=None):
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params or ())
            return cursor.fetchone()
    finally:
        conn.close()


def fetch_all(sql, params=None):
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params or ())
            return cursor.fetchall()
    finally:
        conn.close()


def execute(sql, params=None):
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params or ())
            conn.commit()
            return cursor.lastrowid
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def execute_many_in_transaction(operations):
    """
    operations: [(sql, params), ...]
    """
    conn = get_db()
    try:
        last_id = None
        with conn.cursor() as cursor:
            for sql, params in operations:
                cursor.execute(sql, params or ())
                last_id = cursor.lastrowid
            conn.commit()
            return last_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# -----------------------------
# 登录
# -----------------------------

def login_required(view_func):
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if not session.get("admin_username"):
            return redirect(url_for("login"))
        return view_func(*args, **kwargs)

    return wrapper


@app.route("/")
def root():
    if session.get("admin_username"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""

    if not username or not password:
        flash("请输入用户名和密码。", "danger")
        return redirect(url_for("login"))

    admin = fetch_one(
        "SELECT * FROM admin WHERE username = %s LIMIT 1",
        (username,),
    )

    if not admin:
        flash("用户名或密码错误。", "danger")
        return redirect(url_for("login"))

    stored_hash = admin.get("password") or ""

    # 兼容 PHP password_hash 生成的 bcrypt：$2y$ -> $2b$
    normalized_hash = stored_hash.replace("$2y$", "$2b$", 1)

    if not check_password_hash(normalized_hash, password):
        flash("用户名或密码错误。", "danger")
        return redirect(url_for("login"))

    session["admin_username"] = username
    flash("登录成功。", "success")
    return redirect(url_for("dashboard"))


@app.route("/logout")
def logout():
    session.clear()
    schedule_shutdown()
    return render_template("shutdown.html")


# -----------------------------
# SFTP 上传 / 删除
# -----------------------------

@app.route("/admin_uploads/<path:filename>")
@login_required
def uploaded_file(filename):
    if not filename or "/" in filename or "\\" in filename:
        abort(404)

    upload_dir = config["upload"].get("remote_dir")
    if not upload_dir:
        abort(404)

    return send_from_directory(upload_dir, filename)


def allowed_extensions():
    raw = config["upload"].get("allowed_extensions", "jpg,jpeg,png,webp")
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in allowed_extensions()


def max_upload_bytes() -> int:
    mb = config["upload"].getint("max_size_mb", 8)
    return mb * 1024 * 1024


def generate_remote_filename(original_filename: str, prefix: str) -> str:
    safe = secure_filename(original_filename)
    ext = safe.rsplit(".", 1)[1].lower()
    return f"{prefix}_{uuid.uuid4().hex}.{ext}"


def sftp_client():
    host = config["sftp"].get("host")
    port = config["sftp"].getint("port", 22)
    username = config["sftp"].get("username")
    password = config["sftp"].get("password", fallback=None)
    key_path = config["sftp"].get("key_path", fallback=None)

    transport = paramiko.Transport((host, port))

    if key_path:
        key_path = os.path.expanduser(key_path)
        pkey = paramiko.RSAKey.from_private_key_file(key_path)
        transport.connect(username=username, pkey=pkey)
    else:
        transport.connect(username=username, password=password)

    return transport, paramiko.SFTPClient.from_transport(transport)


def upload_storage_file(file_storage, prefix: str) -> str:
    """
    上传 Flask request.files 里的文件到远程服务器。
    返回数据库保存的 public path，例如 /house/php/uploads/main_xxx.jpg
    """
    if not file_storage or not file_storage.filename:
        raise ValueError("没有选择文件。")

    if not allowed_file(file_storage.filename):
        raise ValueError("文件格式不允许，只允许 jpg、jpeg、png、webp。")

    # 简单大小检查
    file_storage.stream.seek(0, os.SEEK_END)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    if size > max_upload_bytes():
        raise ValueError(f"文件过大，最大允许 {config['upload'].getint('max_size_mb', 8)} MB。")

    remote_filename = generate_remote_filename(file_storage.filename, prefix)
    remote_dir = config["upload"].get("remote_dir").rstrip("/")
    public_prefix = config["upload"].get("public_prefix").rstrip("/")

    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        file_storage.save(tmp.name)
        tmp_path = tmp.name

    transport = None
    sftp = None
    try:
        transport, sftp = sftp_client()
        remote_path = posixpath.join(remote_dir, remote_filename)
        sftp.put(tmp_path, remote_path)
        return posixpath.join(public_prefix, remote_filename)
    finally:
        try:
            if sftp:
                sftp.close()
        finally:
            if transport:
                transport.close()
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def delete_remote_public_path(public_path: str):
    """
    删除远程图片。public_path 是数据库里保存的 /house/php/uploads/xxx.jpg。
    删除失败不阻塞数据库操作。
    """
    if not public_path:
        return

    public_prefix = config["upload"].get("public_prefix").rstrip("/")
    remote_dir = config["upload"].get("remote_dir").rstrip("/")

    if not public_path.startswith(public_prefix):
        return

    filename = public_path.replace(public_prefix, "", 1).lstrip("/")
    if not filename or "/" in filename or "\\" in filename:
        return

    remote_path = posixpath.join(remote_dir, filename)

    transport = None
    sftp = None
    try:
        transport, sftp = sftp_client()
        try:
            sftp.remove(remote_path)
        except FileNotFoundError:
            pass
        except IOError:
            pass
    finally:
        try:
            if sftp:
                sftp.close()
        finally:
            if transport:
                transport.close()


# -----------------------------
# Dashboard
# -----------------------------

@app.route("/dashboard")
@login_required
def dashboard():
    listing_count = fetch_one("SELECT COUNT(*) AS c FROM image")["c"]
    category_count = fetch_one("SELECT COUNT(*) AS c FROM categories")["c"]
    carousel_count = fetch_one("SELECT COUNT(*) AS c FROM image WHERE carousel = 1")["c"]
    return render_template(
        "dashboard.html",
        listing_count=listing_count,
        category_count=category_count,
        carousel_count=carousel_count,
    )


# -----------------------------
# 房源管理
# -----------------------------

@app.route("/listings")
@login_required
def listings():
    rows = fetch_all(
        """
        SELECT i.*, c.name AS category_name
        FROM image i
        LEFT JOIN categories c ON i.category_id = c.id
        ORDER BY i.created_at DESC, i.id DESC
        """
    )
    return render_template("listings.html", listings=rows)


@app.route("/listings/create", methods=["GET", "POST"])
@login_required
def listing_create():
    categories = fetch_all("SELECT * FROM categories ORDER BY id ASC")

    if request.method == "GET":
        return render_template("listing_form.html", mode="create", listing=None, categories=categories, gallery=[])

    try:
        title = (request.form.get("title") or "").strip()
        if not title:
            raise ValueError(f"{ui_text('listing_title_label')}不能为空。")

        category_id = request.form.get("category_id") or None
        carousel = 1 if request.form.get("carousel") == "on" else 0
        price = request.form.get("price") or None
        layout = request.form.get("layout") or None
        area = request.form.get("area") or None
        location = request.form.get("location") or None
        description = request.form.get("description") or None

        main_file = request.files.get("main_image")
        image_url = upload_storage_file(main_file, "main")

        conn = get_db()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO image
                    (category_id, title, image_url, carousel, price, layout, area, location, description, created_at)
                    VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    """,
                    (category_id, title, image_url, carousel, price, layout, area, location, description),
                )
                image_id = cursor.lastrowid

                sub_files = request.files.getlist("sub_images")
                for sub_file in sub_files:
                    if not sub_file or not sub_file.filename:
                        continue
                    sub_url = upload_storage_file(sub_file, "sub")
                    cursor.execute(
                        "INSERT INTO image_gallery (image_id, sub_image_url) VALUES (%s, %s)",
                        (image_id, sub_url),
                    )

            conn.commit()
        except Exception:
            conn.rollback()
            # 如果 DB 写入失败，尝试删除已上传主图
            delete_remote_public_path(image_url)
            raise
        finally:
            conn.close()

        flash(f"{ui_text('listing_name')}新增成功。", "success")
        return redirect(url_for("listings"))

    except Exception as e:
        flash(str(e), "danger")
        return render_template("listing_form.html", mode="create", listing=request.form, categories=categories, gallery=[])


@app.route("/listings/<int:listing_id>/edit", methods=["GET", "POST"])
@login_required
def listing_edit(listing_id):
    listing = fetch_one("SELECT * FROM image WHERE id = %s", (listing_id,))
    if not listing:
        abort(404)

    categories = fetch_all("SELECT * FROM categories ORDER BY id ASC")
    gallery = fetch_all("SELECT * FROM image_gallery WHERE image_id = %s ORDER BY id ASC", (listing_id,))

    if request.method == "GET":
        return render_template("listing_form.html", mode="edit", listing=listing, categories=categories, gallery=gallery)

    try:
        title = (request.form.get("title") or "").strip()
        if not title:
            raise ValueError(f"{ui_text('listing_title_label')}不能为空。")

        category_id = request.form.get("category_id") or None
        carousel = 1 if request.form.get("carousel") == "on" else 0
        price = request.form.get("price") or None
        layout = request.form.get("layout") or None
        area = request.form.get("area") or None
        location = request.form.get("location") or None
        description = request.form.get("description") or None

        new_main_url = listing["image_url"]
        old_main_url = listing["image_url"]

        main_file = request.files.get("main_image")
        replace_main = main_file and main_file.filename

        if replace_main:
            new_main_url = upload_storage_file(main_file, "main")

        conn = get_db()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE image
                    SET category_id=%s,
                        title=%s,
                        image_url=%s,
                        carousel=%s,
                        price=%s,
                        layout=%s,
                        area=%s,
                        location=%s,
                        description=%s
                    WHERE id=%s
                    """,
                    (
                        category_id,
                        title,
                        new_main_url,
                        carousel,
                        price,
                        layout,
                        area,
                        location,
                        description,
                        listing_id,
                    ),
                )

                sub_files = request.files.getlist("sub_images")
                for sub_file in sub_files:
                    if not sub_file or not sub_file.filename:
                        continue
                    sub_url = upload_storage_file(sub_file, "sub")
                    cursor.execute(
                        "INSERT INTO image_gallery (image_id, sub_image_url) VALUES (%s, %s)",
                        (listing_id, sub_url),
                    )

            conn.commit()
        except Exception:
            conn.rollback()
            if replace_main:
                delete_remote_public_path(new_main_url)
            raise
        finally:
            conn.close()

        if replace_main and old_main_url != new_main_url:
            delete_remote_public_path(old_main_url)

        flash(f"{ui_text('listing_name')}更新成功。", "success")
        return redirect(url_for("listing_edit", listing_id=listing_id))

    except Exception as e:
        flash(str(e), "danger")
        return redirect(url_for("listing_edit", listing_id=listing_id))


@app.route("/listings/<int:listing_id>/delete", methods=["POST"])
@login_required
def listing_delete(listing_id):
    listing = fetch_one("SELECT * FROM image WHERE id = %s", (listing_id,))
    if not listing:
        abort(404)

    gallery = fetch_all("SELECT * FROM image_gallery WHERE image_id = %s", (listing_id,))

    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM image_gallery WHERE image_id = %s", (listing_id,))
            cursor.execute("DELETE FROM image WHERE id = %s", (listing_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    # DB 删除成功后再清理远程文件
    delete_remote_public_path(listing.get("image_url"))
    for item in gallery:
        delete_remote_public_path(item.get("sub_image_url"))

    flash(f"{ui_text('listing_name')}已删除。", "success")
    return redirect(url_for("listings"))


@app.route("/gallery/<int:gallery_id>/delete", methods=["POST"])
@login_required
def gallery_delete(gallery_id):
    item = fetch_one("SELECT * FROM image_gallery WHERE id = %s", (gallery_id,))
    if not item:
        abort(404)

    execute("DELETE FROM image_gallery WHERE id = %s", (gallery_id,))
    delete_remote_public_path(item.get("sub_image_url"))

    flash(f"{ui_text('listing_sub_images_label')}已删除。", "success")
    return redirect(url_for("listing_edit", listing_id=item["image_id"]))


# -----------------------------
# 分类管理
# -----------------------------

@app.route("/categories", methods=["GET", "POST"])
@login_required
def categories():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        if not name:
            flash(f"{ui_text('category_name_label')}不能为空。", "danger")
        else:
            execute("INSERT INTO categories (name) VALUES (%s)", (name,))
            flash(f"{ui_text('listing_category_label')}新增成功。", "success")
        return redirect(url_for("categories"))

    rows = fetch_all(
        """
        SELECT c.*, COUNT(i.id) AS listing_count
        FROM categories c
        LEFT JOIN image i ON i.category_id = c.id
        GROUP BY c.id
        ORDER BY c.id ASC
        """
    )
    return render_template("categories.html", categories=rows)


@app.route("/categories/<int:category_id>/update", methods=["POST"])
@login_required
def category_update(category_id):
    name = (request.form.get("name") or "").strip()
    if not name:
        flash(f"{ui_text('category_name_label')}不能为空。", "danger")
        return redirect(url_for("categories"))

    execute("UPDATE categories SET name = %s WHERE id = %s", (name, category_id))
    flash(f"{ui_text('listing_category_label')}已更新。", "success")
    return redirect(url_for("categories"))


@app.route("/categories/<int:category_id>/delete", methods=["POST"])
@login_required
def category_delete(category_id):
    count = fetch_one("SELECT COUNT(*) AS c FROM image WHERE category_id = %s", (category_id,))["c"]
    if count > 0:
        flash(
            f"该{ui_text('listing_category_label')}下还有{ui_text('listing_name')}，不能删除。请先移动或删除相关{ui_text('listing_name')}。",
            "danger",
        )
        return redirect(url_for("categories"))

    execute("DELETE FROM categories WHERE id = %s", (category_id,))
    flash(f"{ui_text('listing_category_label')}已删除。", "success")
    return redirect(url_for("categories"))


# -----------------------------
# 网站设置
# -----------------------------

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    setting = fetch_one("SELECT * FROM settings ORDER BY id ASC LIMIT 1")

    if request.method == "GET":
        return render_template("settings.html", setting=setting)

    site_title = request.form.get("site_title") or ""
    latest_listings_title = request.form.get("latest_listings_title") or ""
    featured_listings_title = request.form.get("featured_listings_title") or ""
    enter_hall_text = request.form.get("enter_hall_text") or ""
    view_all_text = request.form.get("view_all_text") or ""
    catalog_title = request.form.get("catalog_title") or ""

    if setting:
        execute(
            """
            UPDATE settings
            SET site_title=%s,
                latest_listings_title=%s,
                featured_listings_title=%s,
                enter_hall_text=%s,
                view_all_text=%s,
                catalog_title=%s
            WHERE id=%s
            """,
            (
                site_title,
                latest_listings_title,
                featured_listings_title,
                enter_hall_text,
                view_all_text,
                catalog_title,
                setting["id"],
            ),
        )
    else:
        execute(
            """
            INSERT INTO settings
            (site_title, latest_listings_title, featured_listings_title, enter_hall_text, view_all_text, catalog_title)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                site_title,
                latest_listings_title,
                featured_listings_title,
                enter_hall_text,
                view_all_text,
                catalog_title,
            ),
        )

    flash("网站设置已保存。", "success")
    return redirect(url_for("settings"))


# -----------------------------
# 启动
# -----------------------------

def open_browser_later(url: str):
    time.sleep(1)
    webbrowser.open(url)


if __name__ == "__main__":
    host = config["app"].get("host", "127.0.0.1")
    port = config["app"].getint("port", 5088)
    url = f"http://{host}:{port}"

    if config["app"].getboolean("auto_open_browser", True):
        threading.Thread(target=open_browser_later, args=(url,), daemon=True).start()

    threading.Thread(target=monitor_browser_clients, daemon=True).start()

    print(f"House Admin started: {url}")
    serve(app, host=host, port=port)
