SELECT 'CREATE DATABASE arise_test'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'arise_test') \gexec
