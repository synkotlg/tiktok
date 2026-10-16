"""Maquette locale de TikTok : reproduit le DOM suppose par la section UI de tiktok_rotate.py.

Elle ne prouve pas que les selecteurs collent au vrai site (seul 'tiktok_rotate.py diagnose' le dit),
mais elle teste les parcours du script. Si TikTok change son DOM, mets d'abord cette maquette a jour
d'apres les HTML de data/debug, puis les selecteurs (skill maj-selecteurs).

Les (des)abonnements passent par /api/commit/follow/user/ (adresse supposee, FOLLOW_API_RE), qui repond en
JSON {status_code, status_msg, follow_status}.

Comptes speciaux : 'ghost' n'existe pas ; s* ignorent le clic Suivre (le bouton reste sur Follow) ;
p* affichent Following, l'API repond 0 mais rien n'est enregistre (le profil montre Follow : limitation
silencieuse) ; r* affichent Following mais l'API refuse (status_code non nul) ; l'API refuse le
desabonnement des u* ; `bios` donne la bio d'un profil (data-e2e="user-bio") ; d* affichent
"Too many attempts" a partir du 3e abonnement ; les comptes de `captcha` affichent un captcha ('home' dans
`captcha` : captcha des la page d'accueil).
"""
import json
from urllib.parse import parse_qs, urlparse

PAGE = """<!doctype html><html><body>
<header><a data-e2e="nav-profile" href="/@%(me)s"><span data-e2e="profile-icon">P</span></a>
<a href="/upload">Upload</a></header>
<main>%(main)s</main>
<script>
async function act(u, on) {
  const r = await fetch('/api/commit/follow/user/?user_id=' + u + '&type=' + on);
  return (await r.json()).status_code;
}
document.addEventListener('click', async e => {
  const b = e.target.closest('button.fb');
  if (!b) return;
  const u = b.dataset.u, t = b.innerText.trim();
  if (/^s\\d/.test(u)) return;
  if (/^[pr]\\d/.test(u)) { b.innerText = 'Following'; await act(u, 1); return; }
  if (/^d\\d/.test(u)) {
    window.nd = (window.nd || 0) + 1;
    if (window.nd > 2) {
      const x = document.createElement('div');
      x.innerText = 'Too many attempts. Try again later.';
      document.body.appendChild(x);
      return;
    }
  }
  if (t === 'Follow' || t === 'Follow back') {
    if (await act(u, 1) === 0) b.innerText = t === 'Follow back' ? 'Friends' : 'Following';
  } else {
    if (await act(u, 0) === 0) b.innerText = t === 'Friends' ? 'Follow back' : 'Follow';
  }
});
%(script)s
</script></body></html>"""

LIST_SCRIPT = """
const ROWS = %(rows)s;
document.querySelector('[data-e2e="followers-count"]').addEventListener('click', () => {
  const d = document.createElement('div');
  d.setAttribute('role', 'dialog');
  d.innerHTML = '<div role="tablist"><div role="tab" aria-selected="false">Following 10</div>'
    + '<div role="tab" aria-selected="true">Followers ' + ROWS.length + '</div></div>'
    + '<div class="list" style="height:300px;overflow-y:auto"><ul></ul></div>';
  document.body.appendChild(d);
  const ul = d.querySelector('ul');
  let shown = 0;
  const more = () => {
    for (const [u, l] of ROWS.slice(shown, shown + 10)) {
      const li = document.createElement('li');
      li.style.height = '60px';
      li.innerHTML = '<div><a href="/@' + u + '"><p>' + u + '</p></a><span>Nick ' + u + '</span></div>'
        + '<div><button class="fb" data-e2e="follow-button" data-u="' + u + '">' + l + '</button></div>';
      ul.appendChild(li);
    }
    shown += 10;
  };
  more();
  const list = d.querySelector('.list');
  list.addEventListener('scroll', () => {
    if (list.scrollTop + list.clientHeight >= list.scrollHeight - 5) setTimeout(more, 100);
  });
});"""


class MockTikTok:
    def __init__(self, me="me"):
        self.me = me
        self.following = set()      # comptes que `me` suit
        self.my_followers = set()   # comptes qui suivent `me`
        self.followers = {}         # source -> liste de ses abonnes (les plus recents d'abord)
        self.captcha = set()
        self.bios = {}              # pseudo -> bio affichee sur son profil

    def label(self, u):
        if u in self.following:
            return "Friends" if u in self.my_followers else "Following"
        return "Follow back" if u in self.my_followers else "Follow"

    def page(self, main, script=""):
        return PAGE % dict(me=self.me, main=main, script=script)

    def profile(self, u):
        if u == "ghost":
            return self.page("<p>Couldn't find this account</p>")
        if u in self.captcha:
            return self.page('<div id="captcha-verify-container-main-page">Drag the slider to fit the puzzle</div>')
        fl = sorted(self.my_followers) if u == self.me else self.followers.get(u, [])
        btn = "" if u == self.me else (f'<button class="fb" data-e2e="follow-button" data-u="{u}">'
                                       f'{self.label(u)}</button><button>Message</button>')
        bio = f'<h2 data-e2e="user-bio">{self.bios[u]}</h2>' if u in self.bios else ""
        main = (f'<div class="info"><div><h1 data-e2e="user-title">{u}</h1>'
                f'<h2 data-e2e="user-subtitle">Nick</h2>{bio}</div><div class="btns">{btn}</div></div>'
                f'<div><strong data-e2e="following-count">10</strong> Following '
                f'<strong data-e2e="followers-count">{len(fl)}</strong><span data-e2e="followers">Followers</span></div>'
                # comptes suggeres : boutons Suivre qui ne doivent jamais etre pris pour celui du profil
                '<section><h3>Suggested accounts</h3><div><a href="/@sugg1">sugg1</a>'
                '<button class="fb" data-u="sugg1">Follow</button></div></section>')
        return self.page(main, LIST_SCRIPT % dict(rows=json.dumps([[x, self.label(x)] for x in fl])))

    def handler(self, route):
        url = urlparse(route.request.url)
        if url.path == "/api/commit/follow/user/":
            q = parse_qs(url.query)
            user, on = q["user_id"][0], q["type"][0] == "1"
            kind = user[0] if user[1:2].isdigit() else ""
            if kind == "r" or (kind == "u" and not on):
                body = {"status_code": 10221, "status_msg": "", "follow_status": int(user in self.following)}
            else:
                if kind != "p":  # p* : accepte en apparence, rien n'est enregistre
                    (self.following.add if on else self.following.discard)(user)
                body = {"status_code": 0, "status_msg": "", "follow_status": int(user in self.following)}
            return route.fulfill(status=200, body=json.dumps(body), content_type="application/json")
        if url.path.startswith("/@"):
            return route.fulfill(status=200, body=self.profile(url.path[2:].strip("/")), content_type="text/html")
        home = self.profile("home") if "home" in self.captcha else self.page("Pour toi")
        return route.fulfill(status=200, body=home, content_type="text/html")
