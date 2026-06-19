-- Runs once at mysqld startup (via --init-file) with full privileges, so it
-- works without knowing the existing root password.
-- Creates the 'admin' MariaDB account the POS app uses (see .env) with full
-- rights, including creating the pos_db database.
CREATE USER IF NOT EXISTS 'admin'@'localhost' IDENTIFIED BY 'admin';
CREATE USER IF NOT EXISTS 'admin'@'127.0.0.1' IDENTIFIED BY 'admin';
ALTER USER 'admin'@'localhost' IDENTIFIED BY 'admin';
ALTER USER 'admin'@'127.0.0.1' IDENTIFIED BY 'admin';
GRANT ALL PRIVILEGES ON *.* TO 'admin'@'localhost' WITH GRANT OPTION;
GRANT ALL PRIVILEGES ON *.* TO 'admin'@'127.0.0.1' WITH GRANT OPTION;
FLUSH PRIVILEGES;
