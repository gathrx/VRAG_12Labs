"""Custom CSS for the Streamlit UI (works with both light and dark Streamlit themes)."""

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

html, body, [class*="css"], .stMarkdown, button, input, textarea { font-family: 'Inter', system-ui, sans-serif; }
.block-container { padding-top: 2.2rem; padding-bottom: 3rem; max-width: 1500px; }

.vr-hero { padding: 1.4rem 1.6rem; border-radius: 16px; margin-bottom: 1.2rem;
  background: linear-gradient(135deg, rgba(99,102,241,.16), rgba(16,185,129,.12));
  border: 1px solid rgba(127,127,127,.18); }
.vr-hero h1 { font-size: 1.65rem; margin: 0 0 .25rem 0; font-weight: 700; }
.vr-hero p { margin: 0; opacity: .8; }

.vr-title { display:flex; align-items:center; gap:.6rem; flex-wrap: wrap; margin-bottom: .3rem; }
.vr-title h2 { margin: 0; font-size: 1.35rem; font-weight: 650; word-break: break-all; }

.vr-badge { display:inline-flex; align-items:center; gap:.3rem; padding: .14rem .6rem; border-radius: 999px;
  font-size: .76rem; font-weight: 600; letter-spacing: .01em; white-space: nowrap; }
.vr-ready { background: rgba(16,185,129,.16); color: #059669; }
.vr-busy { background: rgba(245,158,11,.18); color: #b45309; }
.vr-failed { background: rgba(239,68,68,.16); color: #dc2626; }
.vr-idle { background: rgba(127,127,127,.16); }

.vr-ts { display:inline-block; font-variant-numeric: tabular-nums; font-weight: 700; font-size: .95rem;
  padding: .12rem .55rem; border-radius: 8px; background: rgba(99,102,241,.14); color: #4f46e5; }
.vr-src { font-size: .72rem; opacity: .7; margin-left: .35rem; }
.vr-desc { font-size: .9rem; line-height: 1.45; margin: .45rem 0 .55rem 0; }
.vr-note { font-size: .74rem; opacity: .65; margin-top: -.2rem; margin-bottom: .4rem; }
.vr-meta { font-size: .82rem; opacity: .75; }
.vr-nomatch { padding: .8rem 1rem; border-radius: 12px; border: 1px dashed rgba(127,127,127,.45);
  background: rgba(127,127,127,.06); font-size: .92rem; }

.st-key-player_panel { position: sticky; top: 3.8rem; }
[class*="st-key-card_"] { transition: transform .12s ease, box-shadow .12s ease; }
[class*="st-key-card_"]:hover { transform: translateY(-2px); box-shadow: 0 8px 22px rgba(0,0,0,.10); }
[class*="st-key-card_"] img { border-radius: 10px; aspect-ratio: 16/9; object-fit: cover; }

@media (max-width: 900px) {
  .st-key-player_panel { position: static; }
  .block-container { padding-left: 1rem; padding-right: 1rem; }
}
</style>
"""
