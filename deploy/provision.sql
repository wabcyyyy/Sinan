-- ===================== 司南生产库账号开通（T8a）=====================
-- 这一份是**运维脚本**，不是 schema：数据库迁移的真相源仍然是
-- travel-agent-python/app/db/migrations/sql/V*.sql（append-only，由 alembic 版本链执行）。
-- 本文件只做一件事：把"运行"和"改结构"拆成两个最小权限账号，谁都不带 root。
--
-- 谁执行：
--   人（或 deploy/deploy.sh 的 provision 子命令，它做的就是把本文件喂给同一条路径）。
--   root 口令不进应用容器：deploy/docker-compose.prod.yml 只把 MYSQL_ROOT_PASSWORD
--   交给 MySQL 自己的初始化与 healthcheck，应用侧只有 DB_USER / DB_PASSWORD。
--
-- 怎么执行（幂等，可反复跑）：
--   docker compose -f deploy/docker-compose.prod.yml --env-file deploy/secrets/prod.env \
--     exec -T mysql sh -c 'envsubst < /cfg/provision.sql | mysql --no-defaults -uroot -p"$MYSQL_ROOT_PASSWORD" --table'
--   deploy.sh 用的是同一句话（它先把本文件 copy 进容器），所以两种路径的结果一致。
--
-- 为什么里面有 ${...} 而不是直接写名字：数据库名与账号名跟着部署走（隔离演练要用
-- 另一套库名），而单一真源在 env 文件里。占位符由 `envsubst` 展开，**展开后的文件
-- 含口令，绝不入库、绝不留在仓库目录**（deploy/secrets/ 已被 .gitignore:38 忽略）。
-- 口令字符集要求见 deploy/README.md：不得含单引号、反斜杠、双引号（本文件把它们
-- 拼进 SQL 字面量，deploy.sh 的 preflight 会先拒掉）。
--
-- 权限清单的来源（不是"通常给这些"）：
--   * 运行账号：应用只做 ORM 读写。全仓 grep `app/**` 无 CREATE/ALTER/DROP/TRUNCATE/
--     LOCK TABLES/CREATE TEMPORARY/GET_LOCK 的运行期 DDL；软删是 UPDATE
--     （app/db/session.py:113-123 全局钩子），删除用户由服务层发 DELETE。
--     → SELECT/INSERT/UPDATE/DELETE 四类，一条 DDL 都不给。
--   * 迁移账号：结构变更全部来自 app/db/migrations/sql/V*.sql，逐条数过的形态只有
--     CREATE TABLE / ALTER TABLE / DROP TABLE IF EXISTS / INSERT INTO（数据种子，
--     如 V12 的城市字典），外加 alembic 自己的 alembic_version（SELECT/INSERT/UPDATE/
--     DELETE，stamp 会先 DELETE 再 INSERT）与建表时的外键（MySQL 建 FK 需要
--     REFERENCES）。没有 VIEW / TRIGGER / PROCEDURE / EVENT / LOCK TABLES /
--     CREATE TEMPORARY TABLES 的形态，所以这些一项都不给。
--     → 将来某条 V*.sql 需要新权限时，先改这个文件并在 PR 里点名那条迁移。
--
-- 连接来源限制：MySQL 账号按 (user, host) 计，host 填的是 compose 里 sinan-data
-- 网段的 IP 模式（内网 DB 没有反解，MySQL 反解失败时按 IP 匹配，所以 IP 模式可用）。
-- 网段变了（SINAN_DATA_SUBNET）必须同步改 SINAN_DB_HOST_PATTERN，deploy.sh 会比对。

-- 建库由 MySQL 镜像的 MYSQL_DATABASE 负责首次初始化；这句是让"手工建库/换库名重跑"
-- 也走得通（幂等）。字符集与 compose 里 mysqld 的服务端默认一致（utf8mb4 / utf8mb4_unicode_ci）。
CREATE DATABASE IF NOT EXISTS `${SINAN_DB_NAME}`
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- ---------- 运行账号（应用进程唯一使用的身份）----------
CREATE USER IF NOT EXISTS '${SINAN_DB_USER}'@'${SINAN_DB_HOST_PATTERN}'
  IDENTIFIED BY '${SINAN_DB_PASSWORD}';
-- 已存在时也要把口令与连接上限对齐 env（轮换口令就是重跑本文件这一步）。
ALTER USER '${SINAN_DB_USER}'@'${SINAN_DB_HOST_PATTERN}'
  IDENTIFIED BY '${SINAN_DB_PASSWORD}'
  WITH MAX_USER_CONNECTIONS 20;
-- 先收回再授：让"权限集合"完全等于本文件声明的那一份，历史上多授的不会残留。
REVOKE ALL PRIVILEGES, GRANT OPTION FROM '${SINAN_DB_USER}'@'${SINAN_DB_HOST_PATTERN}';
GRANT SELECT, INSERT, UPDATE, DELETE
  ON `${SINAN_DB_NAME}`.* TO '${SINAN_DB_USER}'@'${SINAN_DB_HOST_PATTERN}';

-- ---------- 迁移账号（只在发布窗口用，跑完就退场）----------
CREATE USER IF NOT EXISTS '${SINAN_MIGRATE_USER}'@'${SINAN_DB_HOST_PATTERN}'
  IDENTIFIED BY '${SINAN_MIGRATE_PASSWORD}';
ALTER USER '${SINAN_MIGRATE_USER}'@'${SINAN_DB_HOST_PATTERN}'
  IDENTIFIED BY '${SINAN_MIGRATE_PASSWORD}'
  WITH MAX_USER_CONNECTIONS 4;
REVOKE ALL PRIVILEGES, GRANT OPTION FROM '${SINAN_MIGRATE_USER}'@'${SINAN_DB_HOST_PATTERN}';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, INDEX, REFERENCES
  ON `${SINAN_DB_NAME}`.* TO '${SINAN_MIGRATE_USER}'@'${SINAN_DB_HOST_PATTERN}';

FLUSH PRIVILEGES;

-- ---------- 自检输出（给人看的，不改变任何权限）----------
SELECT CURRENT_USER() AS executed_as, @@version AS mysql_version;
SHOW GRANTS FOR '${SINAN_DB_USER}'@'${SINAN_DB_HOST_PATTERN}';
SHOW GRANTS FOR '${SINAN_MIGRATE_USER}'@'${SINAN_DB_HOST_PATTERN}';
-- 期望：运行账号只有 SELECT/INSERT/UPDATE/DELETE；迁移账号多四项结构权限；
-- 两份都不出现 ALL PRIVILEGES ON *.*、GRANT OPTION、SUPER、FILE、PROCESS。
