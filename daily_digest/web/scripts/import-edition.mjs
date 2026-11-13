import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { parseArgs } from 'node:util';
import { api } from '../worker/api.js';
import { openPreviewDatabase } from './sqlite-storage.mjs';
import { EditionImportError, importEditionIntoDatabase } from './edition-import.mjs';

const ROOT = fileURLToPath(new URL('../../../', import.meta.url));
const USAGE =
  'node daily_digest/web/scripts/import-edition.mjs --db <database.sqlite> --edition <edition.json> [--media <directory>]';

export function parseImportArguments(args, root = ROOT) {
  let parsed;
  try {
    parsed = parseArgs({
      args,
      strict: true,
      allowPositionals: false,
      tokens: true,
      options: {
        db: { type: 'string' },
        edition: { type: 'string' },
        media: { type: 'string' },
        help: { type: 'boolean' },
      },
    });
  } catch {
    throw new EditionImportError('usage', 400);
  }
  const names = parsed.tokens.map((token) => token.name);
  if (new Set(names).size !== names.length) throw new EditionImportError('usage', 400);
  if (parsed.values.help && args.length === 1) return { help: true };
  if (
    parsed.values.help ||
    !parsed.values.db?.trim() ||
    !parsed.values.edition?.trim() ||
    parsed.values.db === ':memory:' ||
    (parsed.values.media !== undefined && !parsed.values.media.trim())
  )
    throw new EditionImportError('usage', 400);
  return {
    databasePath: resolve(root, parsed.values.db),
    editionPath: resolve(root, parsed.values.edition),
    mediaDirectory:
      parsed.values.media === undefined ? undefined : resolve(root, parsed.values.media),
  };
}

export async function runImport(options) {
  let edition;
  const text = await readFile(options.editionPath, 'utf8');
  try {
    edition = JSON.parse(text.replace(/^\uFEFF/, ''));
  } catch {
    throw new EditionImportError('edition_json_invalid', 400);
  }
  const DB = openPreviewDatabase(options.databasePath);
  try {
    return await importEditionIntoDatabase({
      worker: { fetch: (request, environment) => api(request, environment) },
      DB,
      edition,
      mediaDirectory: options.mediaDirectory,
    });
  } finally {
    DB.close();
  }
}

export async function main(args = process.argv.slice(2)) {
  try {
    const options = parseImportArguments(args);
    if (options.help) {
      console.log(USAGE);
      return;
    }
    console.log(JSON.stringify(await runImport(options)));
  } catch (error) {
    const status = error instanceof EditionImportError ? error.status : 500;
    const code = error instanceof EditionImportError ? error.code : 'import_failed';
    console.error(JSON.stringify({ error: code, status }));
    process.exitCode = status === 409 ? 3 : status >= 400 && status < 500 ? 2 : 1;
  }
}

if (process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url)
  await main();
