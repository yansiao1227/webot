import { DatabaseSync } from 'node:sqlite';
import { mkdirSync, readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';

/** The small D1 surface consumed by worker/api.js, backed by an owned local file. */
export function openPreviewDatabase(filename) {
  const path = filename === ':memory:' ? filename : resolve(filename);
  if (path !== ':memory:') mkdirSync(dirname(path), { recursive: true });
  const database = new DatabaseSync(path);
  try {
    database.exec('PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA foreign_keys=ON;');
    const version = database.prepare('PRAGMA user_version').get().user_version;
    if (version > 2) throw new Error('Preview database schema is newer than this application');
    if (version === 0) {
      database.exec('BEGIN IMMEDIATE');
      try {
        database.exec(
          readFileSync(new URL('../migrations/0001_editions.sql', import.meta.url), 'utf8'),
        );
        database.exec('PRAGMA user_version=1; COMMIT;');
      } catch (error) {
        database.exec('ROLLBACK');
        throw error;
      }
    }
    if (version < 2) {
      database.exec(readFileSync(new URL('../migrations/0002_media.sql', import.meta.url), 'utf8'));
      database.exec('PRAGMA user_version=2');
    }
  } catch (error) {
    database.close();
    throw error;
  }
  let closed = false;
  return {
    path,
    prepare(sql) {
      const statement = database.prepare(sql);
      const bound = (values) => ({
        bind(...parameters) {
          return bound(parameters);
        },
        async first(column) {
          const row = statement.get(...values);
          return row ? (column === undefined ? row : (row[column] ?? null)) : null;
        },
        async all() {
          return { success: true, results: statement.all(...values) };
        },
        async run() {
          const result = statement.run(...values);
          return {
            success: true,
            meta: { changes: result.changes, last_row_id: result.lastInsertRowid },
          };
        },
      });
      return bound([]);
    },
    close() {
      if (!closed) {
        database.close();
        closed = true;
      }
    },
  };
}
