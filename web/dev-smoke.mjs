const errors = [];
const originalError = console.error;
console.error = (...args) => { errors.push(args.map(String).join(' ')); originalError.apply(console, args); };
window.addEventListener('error', event => errors.push(event.message));
window.addEventListener('unhandledrejection', event => errors.push(String(event.reason)));

const output = {};
try {
  const {SAMPLES, loadSample, renderReport} = await import('./app.mjs');
  for (const sample of SAMPLES) {
    const host = document.createElement('section');
    document.querySelector('#samples-rendered').append(host);
    const result = renderReport(host, await loadSample(sample));
    // Count actual rendered nodes, independently of the report's array length.
    output[sample.id] = {...result, findings_rendered: host.querySelectorAll('.finding').length, console_errors: []};
  }
} catch (error) { errors.push(String(error)); }
await new Promise(resolve => setTimeout(resolve, 0));
for (const sample of Object.keys(output)) output[sample].console_errors = [...errors];
if (!Object.keys(output).length || errors.length) output.errors = errors;
document.querySelector('#out').textContent = JSON.stringify(output, null, 2);
