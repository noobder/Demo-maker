/* Routing, accounts and the opening page.
   Loaded after the main script, which provides S, api, toast, icon, go, openSettings, buildProviders, ... */
let USER = null, initFor = null;
const PAGES = {landing: $('landing'), auth: $('authPage'), app: $('appShell')};
const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

/* ---------------- pages & routing ---------------- */
function showPage(name) {
  Object.entries(PAGES).forEach(([k, el]) => el.classList.toggle('hidden', k !== name));
  document.body.dataset.mode = name;
  if (name !== 'app') { closeSettings(); closeMenu(); }
  window.scrollTo(0, 0);
}
async function fetchMe() { try { USER = (await api('/api/auth/me', undefined, 'GET')).user; } catch { USER = null; } }
function route() {
  const h = location.hash || '#/';
  if (h.startsWith('#/app')) {
    if (!USER) { location.replace('#/auth'); return; }
    showPage('app'); enterApp(); document.title = 'Demo Maker · Workspace';
  } else if (h.startsWith('#/auth')) {
    if (USER) { location.replace('#/app'); return; }
    showPage('auth'); setAuthTab(h.includes('register') ? 'register' : 'login'); document.title = 'Sign in · Demo Maker';
  } else { showPage('landing'); renderLanding(); document.title = 'Demo Maker · Narrated demo videos, automatically'; }
}
addEventListener('hashchange', route);

function stopEverything() { clearInterval(timer); timer = null; }
function onSessionExpired() {
  if (!USER) return;
  USER = null; stopEverything();
  toast('Your session expired. Please sign in again.', 'info');
  location.hash = '#/auth';
}

/* ---------------- workspace ---------------- */
function enterApp() {
  if (initFor !== null && initFor !== USER.id) { location.reload(); return; } // someone else signed in: start from a clean page
  if (initFor === null) {
    initFor = USER.id; loadState('demo-maker-v2:' + USER.id); connState = {};
    buildProviders(); buildEngine(); syncProviderFields(); loadProject(); go('project');
    if (!PROVIDERS[S.provider].needsKey || prov().key) testConnection(true);
  }
  renderUser(); renderStepper();
}
function renderUser() {
  $('btnUser').textContent = (USER.name.trim()[0] || '?').toUpperCase();
  $('menuName').textContent = USER.name; $('menuEmail').textContent = USER.email;
}
function closeMenu() { $('menu').classList.add('hidden'); $('btnUser').setAttribute('aria-expanded', 'false'); }
$('btnUser').onclick = e => {
  e.stopPropagation(); const open = $('menu').classList.toggle('hidden') === false; $('btnUser').setAttribute('aria-expanded', String(open));
};
document.addEventListener('click', e => { if (!e.target.closest('.user')) closeMenu(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeMenu(); });
$('menuSettings').onclick = () => { closeMenu(); openSettings(); };
$('menuHome').onclick = () => { closeMenu(); location.hash = '#/'; };
$('menuOut').onclick = async () => {
  closeMenu(); try { await api('/api/auth/logout', {}); } catch {}
  USER = null; stopEverything(); location.hash = '#/'; location.reload(); // reload also clears keys and project data held in memory
};

/* ---------------- opening page ---------------- */
function renderLanding() {
  const nav = $('navAuth'), go = USER ? '#/app' : '#/auth';
  nav.href = go; nav.textContent = USER ? 'Open workspace' : 'Sign in'; nav.className = USER ? 'btn sm' : 'btn ghost sm';
  document.querySelectorAll('#ctaGo, [data-cta]').forEach(a => a.setAttribute('href', go));
  const band = document.querySelector('.band a.btn'); band.setAttribute('href', go); band.textContent = USER ? 'Open your workspace' : 'Create your account';
}
document.querySelectorAll('[data-scroll]').forEach(b => b.onclick = () => document.getElementById(b.dataset.scroll)?.scrollIntoView({behavior:'smooth', block:'start'}));
addEventListener('scroll', () => $('landNav').classList.toggle('scrolled', scrollY > 10), {passive:true});
$('mockWave').innerHTML = Array.from({length:30}, (_, i) => `<i style="--i:${i};--h:${14 + Math.round(Math.abs(Math.sin(i * 1.7)) * 28)}px"></i>`).join('');
const io = new IntersectionObserver(es => es.forEach(e => { if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); } }), {threshold:.15});
document.querySelectorAll('.reveal').forEach(el => io.observe(el));

/* ---------------- sign in / create account ---------------- */
const TEXT = {login:['Welcome back', 'Sign in to continue to your workspace.'], register:['Create your account', 'It takes less than a minute.']};
function formError(msg) { $('authErrorText').textContent = msg || ''; $('authError').classList.toggle('show', !!msg); }
function fieldErr(id, msg) { const e = $(id + 'Err'); e.textContent = msg || ''; e.classList.toggle('show', !!msg); $(id).setAttribute('aria-invalid', msg ? 'true' : 'false'); }
function setAuthTab(tab) {
  $(tab === 'register' ? 'tabReg' : 'tabLogin').checked = true;
  $('formLogin').classList.toggle('hidden', tab !== 'login'); $('formReg').classList.toggle('hidden', tab !== 'register');
  [$('authTitle').textContent, $('authSub').textContent] = TEXT[tab];
  formError(''); ['liEmail','liPass','rgName','rgEmail','rgPass'].forEach(id => fieldErr(id, ''));
  history.replaceState(null, '', tab === 'register' ? '#/auth/register' : '#/auth');
  setTimeout(() => $(tab === 'register' ? 'rgName' : 'liEmail').focus({preventScroll:true}), 60);
}
document.querySelectorAll('input[name=authTab]').forEach(r => r.onchange = () => setAuthTab(r.value));
document.querySelectorAll('[data-tab]').forEach(b => b.onclick = () => setAuthTab(b.dataset.tab));
$('authBack').onclick = () => { location.hash = '#/'; };
document.querySelectorAll('[data-eye]').forEach(b => {
  b.innerHTML = icon('eye');
  b.onclick = () => { const i = $(b.dataset.eye), show = i.type === 'password'; i.type = show ? 'text' : 'password'; b.innerHTML = icon(show ? 'eyeoff' : 'eye'); b.setAttribute('aria-label', show ? 'Hide password' : 'Show password'); };
});

function strength(pw) {
  let s = 0;
  if (pw.length >= 8) s++; if (pw.length >= 12) s++;
  if (/[a-z]/.test(pw) && /[A-Z]/.test(pw)) s++; if (/\d/.test(pw) && /[^A-Za-z0-9]/.test(pw)) s++;
  return pw.length < 8 ? Math.min(s, 1) : Math.max(s, 1);
}
$('rgPass').oninput = () => {
  const pw = $('rgPass').value, s = pw ? strength(pw) : 0;
  $('strength').dataset.s = s; fieldErr('rgPass', '');
  $('strengthLabel').textContent = !pw ? 'Use 8+ characters. A mix of letters, numbers and symbols is stronger.' : ['', 'Weak: add more characters', 'Okay', 'Good', 'Strong'][s];
};
function setLoading(btn, on) {
  btn.classList.toggle('loading', on); btn.disabled = on;
  btn.querySelector('.spin')?.remove(); if (on) btn.insertAdjacentHTML('afterbegin', '<span class="spin"></span>');
}
async function submitAuth(kind, ev) {
  ev.preventDefault(); formError('');
  const ids = kind === 'login' ? {email:'liEmail', pass:'liPass'} : {name:'rgName', email:'rgEmail', pass:'rgPass'};
  const name = ids.name ? $(ids.name).value.trim() : '', email = $(ids.email).value.trim(), pw = $(ids.pass).value, bad = [];
  if (ids.name) { fieldErr(ids.name, name ? '' : 'Enter your name.'); if (!name) bad.push(ids.name); }
  fieldErr(ids.email, EMAIL_RE.test(email) ? '' : 'Enter a valid email address.'); if (!EMAIL_RE.test(email)) bad.push(ids.email);
  const pwMsg = kind === 'login' ? (pw ? '' : 'Enter your password.') : (pw.length >= 8 ? '' : 'Use at least 8 characters.'); fieldErr(ids.pass, pwMsg); if (pwMsg) bad.push(ids.pass);
  const card = document.querySelector('.auth-box .card');
  const shake = () => { card.classList.remove('invalid'); void card.offsetWidth; card.classList.add('invalid'); setTimeout(() => card.classList.remove('invalid'), 600); };
  if (bad.length) { shake(); $(bad[0]).focus(); return; }
  const btn = ev.target.querySelector('button[type=submit]'); setLoading(btn, true);
  try {
    const {user} = await api(kind === 'login' ? '/api/auth/login' : '/api/auth/register', kind === 'login' ? {email, password:pw} : {name, email, password:pw});
    USER = user; ev.target.reset(); $('strength').dataset.s = 0;
    toast(kind === 'login' ? `Welcome back, ${user.name.split(' ')[0]}!` : `Welcome, ${user.name.split(' ')[0]}! Your account is ready.`, 'ok');
    location.hash = '#/app';
  } catch (e) { formError(e.message); shake(); }
  finally { setLoading(btn, false); }
}
$('formLogin').onsubmit = e => submitAuth('login', e);
$('formReg').onsubmit = e => submitAuth('register', e);

/* ---------------- start ---------------- */
(async () => { await fetchMe(); route(); })();
