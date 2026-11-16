import {build} from 'esbuild';
import {cpSync, mkdirSync, readFileSync, readdirSync, writeFileSync} from 'node:fs';

await build({entryPoints: ['src/index.js'], bundle: true, format: 'esm', minify: true,
  outfile: 'dist/screen.js', legalComments: 'linked'});
writeFileSync('dist/index.js', `(() => { const src = document.currentScript.src; import(new URL('./screen.js', src).href).catch(error => console.error('Bot Screen failed to load', error)); })();\n`);
mkdirSync('dist/licenses', {recursive: true});
cpSync('node_modules/@novnc/novnc/docs', 'dist/licenses/novnc', {recursive: true, filter: path => !path.endsWith('.md')});
for (const name of readdirSync('dist/licenses/novnc')) {
  if (name.startsWith('LICENSE')) {
    const path = `dist/licenses/novnc/${name}`;
    writeFileSync(path, readFileSync(path, 'utf8').replace(/[\t ]+$/gm, ''));
  }
}
