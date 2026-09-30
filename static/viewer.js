const $ = (id) => document.getElementById(id);
const base = location.pathname.replace(/\/$/, '');
const video = $('player');
let hls = null;
let info = null;
let currentIndex = -1;

function setMessage(message) { $('player-message').textContent = message; $('player-message').hidden = !message; }
$('player-message').addEventListener('click', () => video.play().then(() => setMessage('')).catch(() => {}));
function label(index) { return info.episodes[index]?.label || `Episode ${index + 1}`; }
function updateEpisodeButtons() { document.querySelectorAll('[data-episode]').forEach(button => button.classList.toggle('playing', Number(button.dataset.episode) === currentIndex)); }
function hlsPlayback(index) {
  const url = `${base}/hls/${index}/index.m3u8`;
  if (video.canPlayType('application/vnd.apple.mpegurl')) { video.src = url; return; }
  if (window.Hls?.isSupported()) {
    hls = new Hls({maxBufferLength: 30, liveSyncDurationCount: 3});
    hls.loadSource(url); hls.attachMedia(video);
    hls.on(Hls.Events.MANIFEST_PARSED, () => video.play().then(() => setMessage('')).catch(() => setMessage('Press play to start watching.')));
    hls.on(Hls.Events.ERROR, (_, data) => { if (data.fatal) setMessage('Playback could not start. Please try again in a moment.'); });
    return;
  }
  setMessage('This browser cannot play this video. Try a current Chrome, Firefox, Safari, or Edge browser.');
}
function play(index, forceHls = false) {
  if (hls) { hls.destroy(); hls = null; }
  video.pause(); video.removeAttribute('src'); video.load();
  video.querySelectorAll('track').forEach(track => track.remove());
  currentIndex = index; $('playing').textContent = label(index); updateEpisodeButtons();
  for (const [number, subtitle] of (info.episodes[index].subtitles || []).entries()) {
    const track = document.createElement('track'); track.kind = 'subtitles'; track.label = subtitle.label; track.srclang = subtitle.language; track.src = `${base}/subtitle/${index}/${subtitle.index}.vtt`;
    track.default = number === 0 && subtitle.language === 'en'; video.append(track);
  }
  setMessage('Preparing playback…');
  if (!forceHls && info.episodes[index].format === 'mp4') video.src = `${base}/stream/${index}`;
  else hlsPlayback(index);
  if (!hls) video.play().then(() => setMessage('')).catch(() => setMessage('Press play to start watching.'));
}
video.addEventListener('playing', () => setMessage(''));
video.addEventListener('error', () => {
  if (currentIndex >= 0 && info?.episodes[currentIndex].format === 'mp4' && !hls && !video.dataset.fallback) { video.dataset.fallback = '1'; play(currentIndex, true); return; }
  setMessage('This video could not be played right now.');
});
async function start() {
  try {
    const response = await fetch(`${base}/info`);
    if (!response.ok) throw new Error('This link has expired or was revoked.');
    info = await response.json(); document.title = `${info.title} · Shared viewing`;
    $('title').textContent = info.title;
    $('scope-label').textContent = info.scope_label || '';
    $('scope-label').hidden = !info.scope_label;
    $('expiry').textContent = `Available until ${new Date(info.expires_at * 1000).toLocaleString(undefined, {dateStyle: 'long', timeStyle: 'short'})}`;
    if (info.kind === 'series' && info.episodes.length > 1) {
      $('episodes-section').hidden = false;
      for (const episode of info.episodes) {
        const button = document.createElement('button'); button.type = 'button'; button.className = 'episode-item'; button.dataset.episode = episode.index;
        const number = document.createElement('span'); number.className = 'episode-number'; number.textContent = String(episode.index + 1).padStart(2, '0');
        const title = document.createElement('strong'); title.textContent = episode.label;
        const arrow = document.createElement('span'); arrow.textContent = '▶'; arrow.className = 'episode-play';
        button.append(number, title, arrow); button.addEventListener('click', () => { delete video.dataset.fallback; play(episode.index); }); $('episodes').append(button);
      }
    }
    if (info.episodes.length) play(0);
    else setMessage('No playable video is available.');
  } catch (error) { $('title').textContent = 'Link unavailable'; $('viewer-error').textContent = error.message; setMessage(''); }
}
start();
