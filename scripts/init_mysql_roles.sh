#!/bin/bash
set -euo pipefail

# Runs only during first initialization. Existing volumes require an explicit admin migration.
for name in MYSQL_DATABASE APP_DB_NAME APP_DB_USER BUSINESS_DB_USER; do
  value="${!name}"
  [[ "$value" =~ ^[A-Za-z0-9_]+$ ]] || { echo "Invalid database/account identifier" >&2; exit 1; }
done
[[ "$APP_DB_USER" != "$BUSINESS_DB_USER" && "$APP_DB_NAME" != "$MYSQL_DATABASE" ]] || exit 1
[[ -n "$APP_DB_PASSWORD" && -n "$BUSINESS_DB_PASSWORD" ]] || exit 1

# Quote SQL literals without allowing password characters to become SQL syntax.
app_password=${APP_DB_PASSWORD//\'/\'\'}
business_password=${BUSINESS_DB_PASSWORD//\'/\'\'}
MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot <<SQL
SET SESSION sql_mode = 'NO_BACKSLASH_ESCAPES';
CREATE DATABASE IF NOT EXISTS \`$APP_DB_NAME\` CHARACTER SET utf8mb4;
CREATE USER IF NOT EXISTS '$APP_DB_USER'@'%' IDENTIFIED BY '$app_password';
GRANT ALL PRIVILEGES ON \`$APP_DB_NAME\`.* TO '$APP_DB_USER'@'%';
CREATE USER IF NOT EXISTS '$BUSINESS_DB_USER'@'%' IDENTIFIED BY '$business_password';
GRANT SELECT ON \`$MYSQL_DATABASE\`.retail_customers TO '$BUSINESS_DB_USER'@'%';
GRANT SELECT ON \`$MYSQL_DATABASE\`.retail_products TO '$BUSINESS_DB_USER'@'%';
GRANT SELECT ON \`$MYSQL_DATABASE\`.retail_orders TO '$BUSINESS_DB_USER'@'%';
GRANT SELECT ON \`$MYSQL_DATABASE\`.retail_order_items TO '$BUSINESS_DB_USER'@'%';
GRANT SELECT ON \`$MYSQL_DATABASE\`.retail_refunds TO '$BUSINESS_DB_USER'@'%';
SQL
