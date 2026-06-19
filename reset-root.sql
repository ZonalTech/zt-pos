-- Reset the MariaDB 'root' password to 'admin' and (re)create the app 'admin'
-- account with normal password auth — without needing the current root password.
-- Run by mysqld via --init-file at startup (see reset-root.bat), which executes
-- with full privileges before the server accepts connections.
--
-- IDENTIFIED VIA mysql_native_password forces password auth, overriding any
-- gssapi/kerberos plugin a previous setup may have left on these accounts.

ALTER USER 'root'@'localhost' IDENTIFIED VIA mysql_native_password USING PASSWORD('admin');

CREATE USER IF NOT EXISTS 'admin'@'localhost' IDENTIFIED VIA mysql_native_password USING PASSWORD('admin');
CREATE USER IF NOT EXISTS 'admin'@'127.0.0.1' IDENTIFIED VIA mysql_native_password USING PASSWORD('admin');
ALTER  USER 'admin'@'localhost' IDENTIFIED VIA mysql_native_password USING PASSWORD('admin');
ALTER  USER 'admin'@'127.0.0.1' IDENTIFIED VIA mysql_native_password USING PASSWORD('admin');
GRANT ALL PRIVILEGES ON *.* TO 'admin'@'localhost' WITH GRANT OPTION;
GRANT ALL PRIVILEGES ON *.* TO 'admin'@'127.0.0.1' WITH GRANT OPTION;
FLUSH PRIVILEGES;
