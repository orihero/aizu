/* ============================================================
   AIZU landing — English dictionary (the SOURCE locale).
   Classic script, loaded in <head> before landing/js/i18n.js.

   English is authoritative: every key defined here must exist,
   and i18n.js falls back to this dictionary whenever another
   locale is missing a key. Keep the markup in index.html in
   sync with these strings — the data-i18n attributes overwrite
   whatever the HTML ships with, so a drift here is invisible
   until the copy is edited in the wrong place.

   Values ending in ".html" are injected as innerHTML (only
   <br> and <a> are ever used); everything else is textContent.
   ============================================================ */

window.CS_I18N = window.CS_I18N || {};

window.CS_I18N.en = {
  /* ---- document head ---- */
  'meta.title': 'AIZU — a signal to act',
  'meta.description':
    'Tell AIZU what you sell and who you serve. It returns a short list of qualified, ready-to-buy customers, every week.',

  /* ---- nav ---- */
  'nav.home': 'AIZU home',
  'nav.why': 'Why AIZU',
  'nav.where': 'Where we look',
  'nav.how': 'How it behaves',
  'nav.pricing': 'Pricing',
  'nav.faq': 'FAQ',
  'nav.login': 'Log in',
  'nav.cta': 'Start free',
  'nav.lang': 'Language',
  'nav.lang.en': 'English',
  'nav.lang.ru': 'Russian',
  'nav.lang.uz': 'Uzbek',

  /* ---- hero ---- */
  'hero.heading.html': 'Meet customers<br>while they decide',
  'hero.lead':
    'Tell AIZU what you sell and who you serve. It returns a short list of ready-to-buy customers, every week.',
  'hero.cta': 'Start free',
  'hero.tile.ask': 'Ask',
  'hero.tile.match': 'Match',

  /* ---- core-hr (the orbiting feed) ---- */
  'corehr.heading.html': 'Someone is searching<br>for what you sell',
  'corehr.sub':
    'Right now, on some platform you don’t have time to watch. AIZU collects them from every social and brings you the ones ready to buy.',
  'corehr.cta': 'See how it works',

  'card.reply': 'Reply',

  'card.ig1.body':
    'found this dinner set at a market — anyone know a maker who does these?',
  'card.ig1.likes': '128 likes',
  'card.ig1.time': '2 minutes ago',
  'card.ig1.alt': 'A stack of handmade ceramic bowls and plates',

  'card.li1.role': 'Ops Lead · 6m',
  'card.li1.body': 'Can anyone recommend a customs broker for EU shipments?',
  'card.li1.comments': '8 comments',

  'card.x1.handle': '@jonasw · 3m',
  'card.x1.body':
    'anyone selling a used camera body? this is the one I’m after',
  'card.x1.alt': 'A DSLR camera body on a table',

  'card.rd1.sub': '· 24m',
  'card.rd1.body': 'Is this a fair quote for a bathroom retile?',
  'card.rd1.alt': 'A bathroom with grey subway tiles',

  'card.yt1.time': '15 minutes ago',
  'card.yt1.body': 'which lens did you use here? looking to buy one',

  'card.tg1.body': 'does anyone have a cleaner they trust for a 2-bed?',

  'card.ig2.body': 'need eco packaging for a small candle brand — recs?',
  'card.ig2.likes': '64 likes',
  'card.ig2.time': '22 minutes ago',
  'card.ig2.alt': 'Plain kraft cardboard packaging boxes',

  'card.li2.role': 'Founder · 9m',
  'card.li2.body':
    'Looking for a bookkeeper who works with early-stage startups.',
  'card.li2.comments': '12 comments',

  'card.x2.handle': '@kaiton · 27m',
  'card.x2.body':
    'who’s your go-to for contract review? need it done this week',

  'card.rd2.sub': '· 11m',
  'card.rd2.body': 'Best desk chair that actually lasts?',
  'card.rd2.alt': 'A mesh-backed desk chair beside a wooden desk',

  'card.yt2.time': '35 minutes ago',
  'card.yt2.body': 'where can I get this desk? been searching for weeks',

  'card.tg2.body': 'need a photographer for a small event next month',
  'card.tg2.alt': 'A photographer shooting at an outdoor event',

  /* ---- bento ---- */
  'bento.title': 'Built for people who sell direct',
  'bento.sub.html':
    'E-commerce and DTC brands, agencies, service providers,<br>coaches and creators, B2B sellers.',

  'bento.c1.title': 'One brief, every check',
  'bento.c1.body':
    'Write what you sell and who you serve once. AIZU checks every candidate against it, not the other way around.',
  'bento.c1.report': 'Match Report',
  'bento.c1.weekly': 'Weekly',
  'bento.day.mon': 'Mon',
  'bento.day.tue': 'Tue',
  'bento.day.wed': 'Wed',
  'bento.day.thu': 'Thu',
  'bento.day.fri': 'Fri',

  'bento.c2.title': 'Know what a lead cost you',
  'bento.c2.body':
    'CPL trend, channel comparison, spend by stage. The numbers that decide where to spend next.',
  'bento.roller.1': 'Cost per lead, week by week',
  'bento.roller.2': 'Compare spend by channel',
  'bento.roller.3': 'See where spend converts',

  'bento.c3.title': 'Careful by construction',
  'bento.c3.body':
    'Daytime pacing, spend caps, cooperative pause. Every run stays inside the limits you set.',

  'bento.c4.title': 'Every lead, one drawer',
  'bento.c4.body':
    'Who they are, what they asked for in their own words, when they asked, and a direct way to respond.',
  'bento.leads': 'Leads',
  'bento.seeall': 'See all',
  'bento.tab.new': 'New',
  'bento.tab.qualified': 'Qualified',
  'bento.tab.contacted': 'Contacted',
  'bento.row.new.name': 'New lead',
  'bento.row.new.sub': 'Just matched',
  'bento.row.new.aria': 'Message new lead',
  'bento.row.qualified.name': 'Qualified',
  'bento.row.qualified.sub': 'Ready to buy',
  'bento.row.qualified.aria': 'Message qualified lead',
  'bento.row.contacted.name': 'Contacted',
  'bento.row.contacted.sub': 'Reply sent',
  'bento.row.contacted.aria': 'Message contacted lead',
  'bento.response': 'Response Rate',
  'bento.seg.daily': 'Daily',
  'bento.seg.weekly': 'Weekly',
  'bento.seg.monthly': 'Monthly',

  'bento.c5.title': 'Roles that actually mean something',
  'bento.c5.body':
    'Owner, admin, member, viewer. Each sees exactly what their role allows, enforced on the server.',

  /* ---- integrations ---- */
  'integrations.title.html': 'Six places<br>people ask',
  'integrations.1.name': 'Social posts',
  'integrations.1.desc': 'Comments under posts about what you sell.',
  'integrations.2.name': 'Professional network',
  'integrations.2.desc': 'Buying signals in professional threads.',
  'integrations.3.name': 'Public feed',
  'integrations.3.desc': 'Public asks, in real time.',
  'integrations.4.name': 'Video comments',
  'integrations.4.desc': 'Comments under reviews and how-tos.',
  'integrations.5.name': 'Community threads',
  'integrations.5.desc': 'Recommendation threads, niche by niche.',
  'integrations.6.name': 'Messaging groups',
  'integrations.6.desc': 'Open groups where buyers ask first.',

  /* ---- testimonials (behaviour cards) ---- */
  'testimonials.title': 'How AIZU behaves',
  'testimonials.lead.html': 'Not promises. What actually runs,<br>every time.',
  'testimonials.prev': 'Previous testimonial',
  'testimonials.next': 'Next testimonial',
  'testimonials.1.name': 'Attach, never launch',
  'testimonials.1.role': 'Session safety',
  'testimonials.1.quote':
    'AIZU only ever attaches to a browser session you already opened. It never launches or owns one itself.',
  'testimonials.2.name': 'Daytime pacing',
  'testimonials.2.role': 'Rate discipline',
  'testimonials.2.quote':
    'Every run paces itself to normal hours. No overnight bursts, no three-in-the-morning spikes.',
  'testimonials.3.name': 'Spend caps',
  'testimonials.3.role': 'Cost control',
  'testimonials.3.quote':
    'Every model call in a run is budgeted against a cap you set on the campaign. Not an afterthought.',

  /* ---- plans ---- */
  'plans.title': 'Pay for customers, not software',
  'plans.sub':
    'Every plan delivers the same thing: qualified, ready-to-buy customers, every month. The only question is how many you can handle.',
  'plans.period': '/mo',
  'plans.badge': 'Most popular',
  'plans.free.name': 'Free',
  'plans.free.note': 'no card needed',
  'plans.free.leads': 'qualified leads a month',
  'plans.free.cta': 'Start free',
  'plans.starter.name': 'Starter',
  'plans.starter.year': '$249 billed yearly',
  'plans.starter.leads': 'qualified leads a month',
  'plans.starter.cta': 'Choose Starter',
  'plans.pro.name': 'Pro',
  'plans.pro.year': '$1,490 billed yearly',
  'plans.pro.leads': 'qualified leads a month',
  'plans.pro.cta': 'Choose Pro',
  'plans.scale.name': 'Scale',
  'plans.scale.amount': 'Custom',
  'plans.scale.year': 'negotiated cap',
  'plans.scale.number': 'Your number',
  'plans.scale.leads': 'agreed up front',
  'plans.scale.cta': 'Talk to sales',
  'plans.foot':
    'Leads are qualified customers delivered to your account each month. Upgrade, downgrade, or cancel anytime. No contracts, no lock-in.',

  /* ---- faq ---- */
  'faq.title.html': 'Fair questions,<br>straight answers',
  'faq.note.html':
    'Can’t find your answer? Write to <a href="mailto:hello@aizu.uz">hello@aizu.uz</a>. A person replies.',
  'faq.1.q': 'Where do the customers come from?',
  'faq.1.a':
    'From public intent signals: people already asking, in public, for exactly what you offer. AIZU’s job is to surface the ones that match your business while the moment is still live. What you get is the outcome: a qualified, ready-to-buy customer. The finding is our work, not yours.',
  'faq.2.q': 'What exactly do I get with each lead?',
  'faq.2.a':
    'Each lead includes who they are, what they asked for in their own words, when they asked, and a direct way to respond. Enough to reach out the same day, with context. No guesswork, no digging.',
  'faq.3.q': 'Is this cold outreach or a bought list?',
  'faq.3.a':
    'No. AIZU doesn’t sell contact databases, and it doesn’t blast strangers. Every lead is a person who has already signaled, in public and on their own, that they’re looking for what you sell. You’re not interrupting anyone. You’re answering.',
  'faq.4.q': 'How do I know the leads are actually qualified?',
  'faq.4.a':
    'Because qualification is the product. Every lead matches what you sell and who you serve, and shows real, current intent to buy. If a lead isn’t a fit, tell us; your list gets sharper.',
  'faq.5.q': 'How fast do I see results?',
  'faq.5.a':
    'Your first leads typically arrive within days of describing your business, and then they keep coming, week after week. Start on Free: ten qualified leads a month is enough to feel the difference before you spend a dollar.',
  'faq.6.q': 'Can I cancel anytime?',
  'faq.6.a':
    'Yes. Monthly plans cancel with one click, effective at the end of your billing period. No contracts, no exit calls. The Free plan needs no card at all.',

  /* ---- footer ---- */
  'footer.tagline.html':
    'AIZU 合図 is a signal to act<br>— not another platform<br>to check.',
  'footer.nav': 'Footer',
  'footer.col.product': 'Product',
  'footer.col.features': 'Features',
  'footer.col.pricing': 'Pricing',
  'footer.col.resources': 'Resources',
  'footer.link.why': 'Why AIZU',
  'footer.link.how': 'How it works',
  'footer.link.pricing': 'Pricing',
  'footer.link.faq': 'FAQ',
  'footer.link.docs': 'Docs',
  'footer.link.privacy': 'Privacy',
  'footer.link.terms': 'Terms',
  'footer.social': 'Follow us'
};
