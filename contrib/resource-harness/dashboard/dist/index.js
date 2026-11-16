(() => { const src = document.currentScript.src; import(new URL('./screen.js', src).href).catch(error => console.error('Bot Screen failed to load', error)); })();
