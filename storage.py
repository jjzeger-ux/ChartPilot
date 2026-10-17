"""Transactional workspace snapshots; SQLite locally, PostgreSQL on Vercel."""
import json
import os
import sqlite3
from pathlib import Path


def empty_state():
    return {'encounters': {}, 'order': [], 'active': None, 'intake': {},
            'scribe': {}, 'transcript': '', 'seq': 0}


class WorkspaceStore:
    def __init__(self, path, workspace, write=False):
        self.workspace, self.write = workspace, write
        url = os.environ.get('DATABASE_URL', '').strip()
        self.postgres = bool(url)
        if self.postgres:
            import psycopg
            self.db = psycopg.connect(url, connect_timeout=8, autocommit=True)
            self.db.execute('CREATE TABLE IF NOT EXISTS hermes_workspaces (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
            self.db.execute('BEGIN')
            if write:
                self.db.execute('INSERT INTO hermes_workspaces VALUES (%s, %s) ON CONFLICT (id) DO NOTHING',
                                (workspace, json.dumps(empty_state())))
            row = self.db.execute('SELECT data FROM hermes_workspaces WHERE id = %s' + (' FOR UPDATE' if write else ''), (workspace,)).fetchone()
        else:
            if os.environ.get('VERCEL'):
                raise RuntimeError('Hosted storage is not configured')
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(path, timeout=10)
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('CREATE TABLE IF NOT EXISTS hermes_workspaces (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
            self.db.commit()
            if write:
                self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('SELECT data FROM hermes_workspaces WHERE id = ?', (workspace,)).fetchone()
        self.state = json.loads(row[0]) if row else empty_state()

    def commit(self):
        if self.write:
            query = 'INSERT INTO hermes_workspaces (id, data) VALUES (?, ?) ON CONFLICT (id) DO UPDATE SET data = excluded.data'
            if self.postgres: query = query.replace('?', '%s')
            self.db.execute(query, (self.workspace, json.dumps(self.state)))
        self.db.commit()

    def close(self):
        self.db.rollback()
        self.db.close()
