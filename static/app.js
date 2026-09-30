(function () {
  const input = document.getElementById('file');
  const drop = document.getElementById('drop');
  const picked = document.getElementById('picked');
  const form = document.getElementById('upload-form');
  if (!input) return;
  function show() {
    picked.textContent = input.files.length ? input.files[0].name + ' (' + Math.round(input.files[0].size / 1024) + ' KB)' : '';
  }
  input.addEventListener('change', show);
  ['dragenter', 'dragover'].forEach(e => drop.addEventListener(e, ev => { ev.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach(e => drop.addEventListener(e, ev => { ev.preventDefault(); drop.classList.remove('over'); }));
  drop.addEventListener('drop', ev => { if (ev.dataTransfer.files.length) { input.files = ev.dataTransfer.files; show(); } });
  form.addEventListener('submit', () => { form.querySelector('button').disabled = true; form.querySelector('button').textContent = 'Uploading...'; });
})();
