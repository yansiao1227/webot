import { spawnSync } from 'node:child_process';
import { mkdir, readdir, readFile, writeFile, copyFile } from 'node:fs/promises';
import { resolve, relative, extname } from 'node:path';
import { build } from 'esbuild';
const run = spawnSync(process.execPath, ['node_modules/vite/bin/vite.js', 'build'], {
  stdio: 'inherit',
});
if (run.status !== 0) process.exit(run.status || 1);
const assets = {};
async function walk(dir) {
  for (const e of await readdir(dir, { withFileTypes: true })) {
    const path = resolve(dir, e.name);
    if (e.isDirectory()) await walk(path);
    else {
      const name =
        '/' + relative(resolve('dist/client'), path).replaceAll(String.fromCharCode(92), '/');
      assets[name] = {
        body: (await readFile(path)).toString('base64'),
        type:
          {
            '.html': 'text/html; charset=utf-8',
            '.js': 'text/javascript; charset=utf-8',
            '.css': 'text/css; charset=utf-8',
            '.svg': 'image/svg+xml',
            '.png': 'image/png',
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.webp': 'image/webp',
          }[extname(path)] || 'application/octet-stream',
      };
    }
  }
}
await walk(resolve('dist/client'));
await mkdir('build', { recursive: true });
await mkdir('dist/server', { recursive: true });
await writeFile('build/assets.js', 'export default ' + JSON.stringify(assets));
await build({
  entryPoints: ['worker/index.js'],
  outfile: 'dist/server/index.js',
  bundle: true,
  format: 'esm',
  platform: 'browser',
  target: 'es2022',
  minify: true,
});
try {
  await mkdir('dist/.openai', { recursive: true });
  await copyFile('.openai/hosting.json', 'dist/.openai/hosting.json');
} catch (e) {
  if (e.code !== 'ENOENT') throw e;
}
await mkdir('dist/migrations', { recursive: true });
await copyFile('migrations/0001_editions.sql', 'dist/migrations/0001_editions.sql');
console.log('Built Sites Worker ESM: dist/server/index.js');
