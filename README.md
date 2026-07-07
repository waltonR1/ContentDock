# Content Admin Flask

一个轻量的本地 Flask 内容管理后台，用来维护已有 MySQL / MariaDB 数据库里的内容、分组、图片和展示文案。

默认适配现有 `real_estate_db` 表结构，界面文案已经配置化，可以通过 `config.ini` 把“内容管理”“字段1”“封面图”等名称改成任意业务叫法。

## 功能

- 管理员登录 / 退出
- 内容列表
- 新增内容
- 编辑内容
- 删除内容
- 封面图上传
- 附加图上传
- 附加图删除
- 分组管理
- 展示设置管理
- 通过 SFTP 上传图片
- 写入远程或本地 MySQL / MariaDB
- 后台图片预览
- UI 文案配置化

## 初始化

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy config.example.ini config.ini
python app.py
```

打开：

```text
http://127.0.0.1:5088
```

## 配置

复制 `config.example.ini` 为 `config.ini` 后，至少需要修改这些配置：

```ini
[database]
host = your.server.ip
port = 3306
database = real_estate_db
user = house_admin
password = your_db_password

[sftp]
host = your.server.ip
port = 22
username = your_ssh_username
password = your_ssh_password

[upload]
remote_dir = /var/www/html/house/php/uploads
public_prefix = /house/php/uploads
```

`config.ini` 包含真实密码，已被 `.gitignore` 忽略；提交代码时请提交 `config.example.ini`，不要提交 `config.ini`。

## UI 文案

后台显示文字集中放在 `[ui]` 中。当前配置是偏通用的内容管理：

```ini
[ui]
app_name = House Admin
dashboard_label = 仪表盘
listing_name = 内容
listing_nav = 内容管理
listing_title_label = 名称
listing_category_label = 分组
listing_price_label = 字段1
listing_layout_label = 字段2
listing_area_label = 字段3
listing_location_label = 字段4
listing_description_label = 详细说明
listing_main_image_label = 封面图
listing_sub_images_label = 附加图
category_nav = 分组管理
settings_nav = 展示设置
```

这里只改变后台界面文案，不改变数据库字段名。数据库仍使用现有的 `image.price`、`image.layout`、`image.area`、`image.location` 等字段。

## 图片预览

数据库里的图片字段保存前台使用的路径，例如：

```text
/php/uploads/main_xxx.jpg
```

后台本地运行在 `http://127.0.0.1:5088` 时，不能直接访问这个前台路径。程序会按下面顺序生成预览地址：

1. 如果 `[display] image_base_url` 已配置，则拼接成完整图片 URL。
2. 如果未配置，则尝试从 `[upload] remote_dir` 本地代理读取图片。

本地 phpstudy 示例：

```ini
[upload]
remote_dir = D:/phpstudy_pro/WWW/house-main/php/uploads
public_prefix = /php/uploads

[display]
image_base_url =
```

线上站点示例：

```ini
[display]
image_base_url = https://example.com
```

## 数据表约定

当前代码默认使用这些表：

- `admin`：后台管理员
- `image`：主要内容
- `image_gallery`：附加图片
- `categories`：分组
- `settings`：展示设置

后台界面可以通用化，但如果要改数据库表名或字段名，需要同步修改 `app.py` 里的 SQL。

## 打包 exe

```bash
pip install pyinstaller
pyinstaller --noconfirm --onefile --name content-admin ^
  --add-data "templates;templates" ^
  --add-data "static;static" ^
  app.py
```

生成：

```text
dist/content-admin.exe
```

把 `config.ini` 放在 exe 同目录。

## 数据库建议

前台只读账号：

```sql
CREATE USER 'content_readonly'@'localhost' IDENTIFIED BY '前台强密码';
GRANT SELECT ON real_estate_db.* TO 'content_readonly'@'localhost';
FLUSH PRIVILEGES;
```

后台账号，只允许可信 IP：

```sql
CREATE USER 'content_admin'@'你的公网IP' IDENTIFIED BY '后台强密码';
GRANT SELECT, INSERT, UPDATE, DELETE ON real_estate_db.* TO 'content_admin'@'你的公网IP';
FLUSH PRIVILEGES;
```

建议给 `admin` 表补主键：

```sql
ALTER TABLE admin ADD COLUMN id INT NOT NULL AUTO_INCREMENT PRIMARY KEY FIRST;
ALTER TABLE admin ADD UNIQUE KEY uk_admin_username (username);
```

建议统一字符集：

```sql
ALTER TABLE categories CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
ALTER TABLE settings CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```
