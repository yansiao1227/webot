import { readFile, stat } from 'node:fs/promises';
import { resolve } from 'node:path';
import { randomBytes } from 'node:crypto';
import { contentHash, validateEdition, InputError } from '../worker/api.js';

export class EditionImportError extends Error {
  constructor(code, status = 500) {
    super('Local edition import failed: ' + status + ' ' + code);
    this.code = code;
    this.status = status;
  }
}

const sha256 = async (bytes) =>
  Buffer.from(await crypto.subtle.digest('SHA-256', bytes)).toString('hex');

/** Import through the same Worker API used by HTTP, without creating a listener. */
export async function importEditionIntoDatabase({ worker, DB, edition, mediaDirectory }) {
  if (typeof worker?.fetch !== 'function' || !DB)
    throw new EditionImportError('import_environment_invalid');
  try {
    validateEdition(edition, edition?.date);
    if ((await contentHash(edition)) !== edition.content_hash)
      throw new EditionImportError('edition_hash_invalid', 400);
  } catch (error) {
    if (error instanceof EditionImportError) throw error;
    if (error instanceof InputError) throw new EditionImportError('edition_invalid', error.status);
    throw error;
  }

  const environment = { DB };
  const url = 'http://localhost/api/editions/' + edition.date;
  const existing = await worker.fetch(new Request(url), environment);
  if (existing.status !== 404) {
    if (!existing.ok) throw new EditionImportError('edition_lookup_failed', existing.status);
    const value = await existing.json();
    if (value.content_hash !== edition.content_hash)
      throw new EditionImportError('edition_conflict', 409);
  }

  // Ephemeral import credential is never assigned to the preview HTTP environment.
  const secret = randomBytes(32).toString('hex');
  const writeEnvironment = { DB, DIGEST_PUBLISH_TOKEN: secret };
  const digests = new Set(edition.sources.map((source) => source.image?.asset_id).filter(Boolean));
  for (const digest of digests) {
    const mediaUrl = 'http://localhost/media/' + digest;
    let saved = await worker.fetch(new Request(mediaUrl), environment);
    if (saved.status === 404) {
      if (!mediaDirectory) throw new EditionImportError('media_directory_required', 400);
      const filename = resolve(mediaDirectory, digest + '.webp');
      const info = await stat(filename);
      if (!info.isFile() || info.size > 512 * 1024)
        throw new EditionImportError('media_file_invalid', info.size > 512 * 1024 ? 413 : 400);
      const uploaded = await worker.fetch(
        new Request('http://localhost/api/media/' + digest, {
          method: 'PUT',
          headers: { 'content-type': 'image/webp', authorization: 'Bearer ' + secret },
          body: await readFile(filename),
        }),
        writeEnvironment,
      );
      if (!uploaded.ok) throw new EditionImportError('media_import_failed', uploaded.status);
      saved = await worker.fetch(new Request(mediaUrl), environment);
    }
    if (!saved.ok || (await sha256(await saved.arrayBuffer())) !== digest)
      throw new EditionImportError('media_readback_failed');
  }

  const response = await worker.fetch(
    new Request(url, {
      method: 'PUT',
      headers: { 'content-type': 'application/json', authorization: 'Bearer ' + secret },
      body: JSON.stringify(edition),
    }),
    writeEnvironment,
  );
  if (!response.ok)
    throw new EditionImportError(
      response.status === 409 ? 'edition_conflict' : 'edition_import_failed',
      response.status,
    );
  const saved = await worker.fetch(new Request(url), environment);
  if (!saved.ok) throw new EditionImportError('edition_readback_failed');
  const value = await saved.json();
  if (
    value.date !== edition.date ||
    value.content_hash !== edition.content_hash ||
    (await contentHash(value)) !== edition.content_hash
  )
    throw new EditionImportError('edition_readback_failed');
  return { date: value.date, content_hash: value.content_hash, databasePath: DB.path };
}
