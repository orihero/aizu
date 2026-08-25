/* ============================================================
   AIZU landing — Uzbek (Latin) dictionary.
   Classic script, loaded in <head> before landing/js/i18n.js.

   Mirrors every key in en.js; anything missing falls back to
   the English string.

   Orthography: the modifier letter U+02BB (ʻ) is used for oʻ /
   gʻ and U+2019 (’) for the glottal stop in soʻz / sanʼat, per
   the standard Latin alphabet — never the ASCII apostrophe.
   Both live inside the latin woff2 subset already shipped in
   landing/fonts/, so this locale needs no extra font file.
   ============================================================ */

window.CS_I18N = window.CS_I18N || {};

window.CS_I18N.uz = {
  /* ---- document head ---- */
  'meta.title': 'AIZU — harakatga chorlovchi signal',
  'meta.description':
    'AIZUga nima sotishingizni va kimga sotishingizni ayting. U har hafta sotib olishga tayyor, saralangan mijozlarning qisqa roʻyxatini qaytaradi.',

  /* ---- nav ---- */
  'nav.home': 'AIZU — bosh sahifa',
  'nav.why': 'Nega AIZU',
  'nav.where': 'Manbalar',
  'nav.how': 'Tamoyillar',
  'nav.pricing': 'Narxlar',
  'nav.faq': 'FAQ',
  'nav.login': 'Kirish',
  'nav.cta': 'Bepul boshlash',
  'nav.lang': 'Til',
  'nav.lang.en': 'Inglizcha',
  'nav.lang.ru': 'Ruscha',
  'nav.lang.uz': 'Oʻzbekcha',

  /* ---- hero ---- */
  'hero.heading.html': 'Mijozlar tanlayotgan<br>paytda ular bilan uchrashing',
  'hero.lead':
    'AIZUga nima va kimga sotishingizni ayting. U har hafta sotib olishga tayyor mijozlarning qisqa roʻyxatini qaytaradi.',
  'hero.cta': 'Bepul boshlash',
  'hero.tile.ask': 'Soʻrov',
  'hero.tile.match': 'Moslik',

  /* ---- core-hr (aylanuvchi lenta) ---- */
  'corehr.heading.html': 'Kimdir ayni damda siz<br>sotadigan narsani qidiryapti',
  'corehr.sub':
    'Aynan hozir — siz kuzatishga ulgurmaydigan platformada. AIZU bunday soʻrovlarni barcha ijtimoiy tarmoqlardan yigʻadi va sotib olishga tayyorlarini sizga keltiradi.',
  'corehr.cta': 'Qanday ishlashini koʻring',

  'card.reply': 'Javob berish',

  'card.ig1.body':
    'bu idish-tovoq toʻplamini bozorda koʻrdim — shunday qiladigan ustani biladiganlar bormi?',
  'card.ig1.likes': '128 ta layk',
  'card.ig1.time': '2 daqiqa oldin',
  'card.ig1.alt': 'Qoʻlda yasalgan keramika kosa va likopchalar uyumi',

  'card.li1.role': 'Operatsiyalar rahbari · 6 daq',
  'card.li1.body':
    'YeIga yuk joʻnatish uchun bojxona brokerini kim tavsiya qiladi?',
  'card.li1.comments': '8 ta izoh',

  'card.x1.handle': '@jonasw · 3 daq',
  'card.x1.body':
    'ishlatilgan kamera korpusini kim sotadi? menga aynan shunisi kerak',
  'card.x1.alt': 'Stol ustidagi DSLR kamera korpusi',

  'card.rd1.sub': '· 24 daq',
  'card.rd1.body': 'Hammomga plitka qayta yotqizish uchun bu narx oʻrinlimi?',
  'card.rd1.alt': 'Kulrang gʻisht shaklidagi plitkali hammom',

  'card.yt1.time': '15 daqiqa oldin',
  'card.yt1.body':
    'bu yerda qaysi obyektivdan foydalandingiz? shundayini olmoqchiman',

  'card.tg1.body':
    'ikki xonali uy uchun ishonchli tozalovchi bor odam bormi?',

  'card.ig2.body':
    'kichik sham brendi uchun eko-qadoq kerak — nimani tavsiya qilasiz?',
  'card.ig2.likes': '64 ta layk',
  'card.ig2.time': '22 daqiqa oldin',
  'card.ig2.alt': 'Oddiy kraft kartondan qadoq qutilar',

  'card.li2.role': 'Asoschi · 9 daq',
  'card.li2.body':
    'Boshlangʻich bosqichdagi startaplar bilan ishlaydigan buxgalter qidiryapman.',
  'card.li2.comments': '12 ta izoh',

  'card.x2.handle': '@kaiton · 27 daq',
  'card.x2.body':
    'shartnomani tekshirish uchun kimga murojaat qilasiz? shu hafta kerak',

  'card.rd2.sub': '· 11 daq',
  'card.rd2.body': 'Qaysi ish stuli haqiqatan uzoq xizmat qiladi?',
  'card.rd2.alt': 'Yogʻoch stol yonidagi toʻrli suyanchiqli ish stuli',

  'card.yt2.time': '35 daqiqa oldin',
  'card.yt2.body':
    'bunday stolni qayerdan olsa boʻladi? bir necha haftadan beri qidiryapman',

  'card.tg2.body': 'kelasi oyda kichik tadbir uchun fotograf kerak',
  'card.tg2.alt': 'Ochiq havodagi tadbirda suratga olayotgan fotograf',

  /* ---- bento ---- */
  'bento.title': 'Toʻgʻridan-toʻgʻri sotadiganlar uchun',
  'bento.sub.html':
    'E-commerce va DTC brendlar, agentliklar, xizmat koʻrsatuvchilar,<br>kouchlar va kontent mualliflari, B2B sotuvchilar.',

  'bento.c1.title': 'Bitta brif, barcha tekshiruvlar',
  'bento.c1.body':
    'Nima va kimga sotishingizni bir marta yozing. AIZU har bir nomzodni shunga solishtiradi — aksincha emas.',
  'bento.c1.report': 'Mosliklar',
  'bento.c1.weekly': 'Haftalik',
  'bento.day.mon': 'Du',
  'bento.day.tue': 'Se',
  'bento.day.wed': 'Ch',
  'bento.day.thu': 'Pa',
  'bento.day.fri': 'Ju',

  'bento.c2.title': 'Har bir lid qanchaga tushganini biling',
  'bento.c2.body':
    'CPL dinamikasi, kanallarni solishtirish, bosqichlar boʻyicha xarajat. Keyingi sarfni hal qiladigan raqamlar.',
  'bento.roller.1': 'Lid narxi, haftama-hafta',
  'bento.roller.2': 'Xarajatni kanallar boʻyicha solishtiring',
  'bento.roller.3': 'Xarajat qayerda natija berayotganini koʻring',

  'bento.c3.title': 'Ehtiyotkorlik — tuzilishida',
  'bento.c3.body':
    'Kunduzgi sur’at, xarajat chegaralari, hamkorlikdagi pauza. Har bir ishga tushirish siz belgilagan chegarada qoladi.',

  'bento.c4.title': 'Har bir lid — bitta kartochkada',
  'bento.c4.body':
    'U kim, oʻz soʻzlari bilan nima soʻragan, qachon soʻragan va unga toʻgʻridan-toʻgʻri qanday javob berish mumkin.',
  'bento.leads': 'Lidlar',
  'bento.seeall': 'Barchasi',
  'bento.tab.new': 'Yangi',
  'bento.tab.qualified': 'Saralangan',
  'bento.tab.contacted': 'Bogʻlanilgan',
  'bento.row.new.name': 'Yangi lid',
  'bento.row.new.sub': 'Endigina topildi',
  'bento.row.new.aria': 'Yangi lidga yozish',
  'bento.row.qualified.name': 'Saralangan',
  'bento.row.qualified.sub': 'Sotib olishga tayyor',
  'bento.row.qualified.aria': 'Saralangan lidga yozish',
  'bento.row.contacted.name': 'Bogʻlanilgan',
  'bento.row.contacted.sub': 'Javob yuborildi',
  'bento.row.contacted.aria': 'Bogʻlanilgan lidga yozish',
  'bento.response': 'Javob ulushi',
  'bento.seg.daily': 'Kunlik',
  'bento.seg.weekly': 'Haftalik',
  'bento.seg.monthly': 'Oylik',

  'bento.c5.title': 'Haqiqatan maʼnoga ega rollar',
  'bento.c5.body':
    'Egasi, admin, aʼzo, kuzatuvchi. Har biri roli ruxsat bergan narsani koʻradi — buni server nazorat qiladi.',

  /* ---- integrations ---- */
  'integrations.title.html': 'Odamlar soʻraydigan<br>oltita joy',
  'integrations.1.name': 'Ijtimoiy tarmoq postlari',
  'integrations.1.desc':
    'Siz sotadigan narsa haqidagi postlar ostidagi izohlar.',
  'integrations.2.name': 'Professional tarmoq',
  'integrations.2.desc': 'Professional muhokamalardagi xarid signallari.',
  'integrations.3.name': 'Ommaviy lenta',
  'integrations.3.desc': 'Ochiq soʻrovlar — real vaqtda.',
  'integrations.4.name': 'Video izohlari',
  'integrations.4.desc': 'Sharh va qoʻllanma videolar ostidagi izohlar.',
  'integrations.5.name': 'Hamjamiyat mavzulari',
  'integrations.5.desc': 'Tavsiya mavzulari — nishama-nisha.',
  'integrations.6.name': 'Messenjer guruhlari',
  'integrations.6.desc':
    'Xaridorlar birinchi boʻlib soʻraydigan ochiq guruhlar.',

  /* ---- testimonials (xatti-harakat kartochkalari) ---- */
  'testimonials.title': 'AIZU oʻzini qanday tutadi',
  'testimonials.lead.html':
    'Vaʼda emas. Har safar haqiqatan<br>bajariladigan narsa.',
  'testimonials.prev': 'Oldingi kartochka',
  'testimonials.next': 'Keyingi kartochka',
  'testimonials.1.name': 'Ulanadi, ishga tushirmaydi',
  'testimonials.1.role': 'Sessiya xavfsizligi',
  'testimonials.1.quote':
    'AIZU faqat siz allaqachon ochgan brauzer sessiyasiga ulanadi. Hech qachon oʻzi sessiya ochmaydi va unga egalik qilmaydi.',
  'testimonials.2.name': 'Kunduzgi sur’at',
  'testimonials.2.role': 'Sur’at intizomi',
  'testimonials.2.quote':
    'Har bir ishga tushirish odatdagi ish soatlariga moslashadi. Tungi portlashlar ham, tongning uchidagi keskin oshishlar ham yoʻq.',
  'testimonials.3.name': 'Xarajat chegaralari',
  'testimonials.3.role': 'Xarajat nazorati',
  'testimonials.3.quote':
    'Ishga tushirishdagi har bir model chaqiruvi siz kampaniyaga belgilagan chegaradan hisoblanadi. Keyin oʻylanadigan narsa emas.',

  /* ---- plans ---- */
  'plans.title': 'Dastur uchun emas, mijozlar uchun toʻlang',
  'plans.sub':
    'Barcha tariflar bir narsani beradi: har oy sotib olishga tayyor, saralangan mijozlar. Yagona savol — nechtasini uddalay olasiz.',
  'plans.period': '/oy',
  'plans.badge': 'Eng ommabop',
  'plans.free.name': 'Bepul',
  'plans.free.note': 'karta kerak emas',
  'plans.free.leads': 'saralangan lid — har oyda',
  'plans.free.campaigns': '1 ta kampaniya',
  'plans.free.cta': 'Bepul boshlash',
  'plans.lite.name': 'Lite',
  'plans.lite.year': 'yillik toʻlovda $99',
  'plans.lite.leads': 'saralangan lid — har oyda',
  'plans.lite.campaigns': '3 ta kampaniya',
  'plans.lite.cta': 'Lite tanlash',
  'plans.starter.name': 'Starter',
  'plans.starter.year': 'yillik toʻlovda $249',
  'plans.starter.leads': 'saralangan lid — har oyda',
  'plans.starter.campaigns': 'Cheksiz kampaniyalar',
  'plans.starter.cta': 'Starter tanlash',
  'plans.pro.name': 'Pro',
  'plans.pro.year': 'yillik toʻlovda $1 490',
  'plans.pro.leads': 'saralangan lid — har oyda',
  'plans.pro.campaigns': 'Cheksiz kampaniyalar',
  'plans.pro.cta': 'Pro tanlash',
  'plans.scale.name': 'Scale',
  'plans.scale.amount': 'Individual',
  'plans.scale.year': 'chegara kelishuv asosida',
  'plans.scale.number': 'Sizning raqamingiz',
  'plans.scale.leads': 'oldindan kelishiladi',
  'plans.scale.campaigns': 'Cheksiz kampaniyalar',
  'plans.scale.cta': 'Biz bilan bogʻlaning',
  'plans.foot':
    'Lidlar — har oy hisobingizga tushadigan saralangan mijozlar. Tarifni istalgan vaqtda oshiring, tushiring yoki bekor qiling. Shartnoma ham, bogʻlanib qolish ham yoʻq.',

  /* ---- faq ---- */
  'faq.title.html': 'Halol savollar,<br>toʻgʻri javoblar',
  'faq.note.html':
    'Javob topa olmadingizmi? <a href="mailto:hello@aizu.uz">hello@aizu.uz</a> manziliga yozing. Sizga odam javob beradi.',
  'faq.1.q': 'Mijozlar qayerdan keladi?',
  'faq.1.a':
    'Ommaviy niyat signallaridan: odamlar siz taklif qilayotgan narsani allaqachon ochiq soʻrab turishadi. AIZUning vazifasi — payt hali tirikligida biznesingizga mos keladiganlarini yuzaga chiqarish. Siz natijani olasiz: sotib olishga tayyor, saralangan mijoz. Qidirish — bizning ishimiz, sizniki emas.',
  'faq.2.q': 'Har bir lid bilan aniq nima olaman?',
  'faq.2.a':
    'Har bir lidda: odamga nima kerakligi oddiy soʻzlar bilan, buni qachon soʻragani va oʻzi qoldirgan boʻlsa, aloqa maʼlumotlari. Oʻsha kuniyoq, kontekst bilan yozish uchun yetarli. Taxmin ham, qazish ham kerak emas.',
  'faq.3.q': 'Bu sovuq murojaatmi yoki sotib olingan bazami?',
  'faq.3.a':
    'Yoʻq. AIZU kontakt bazalarini sotmaydi va notanish odamlarga ommaviy xabar yubormaydi. Har bir lid — siz sotadigan narsani qidirayotganini oʻzi, ochiq bildirgan odam. Siz hech kimni bezovta qilmayapsiz. Siz javob beryapsiz.',
  'faq.4.q': 'Lidlar haqiqatan saralanganini qanday bilaman?',
  'faq.4.a':
    'Chunki saralashning oʻzi mahsulot. Har bir lid siz nima va kimga sotishingizga mos keladi hamda haqiqiy, hozirgi xarid niyatini koʻrsatadi. Lid mos kelmasa — bizga ayting, roʻyxatingiz aniqroq boʻladi.',
  'faq.5.q': 'Natijani qanchalik tez koʻraman?',
  'faq.5.a':
    'Birinchi lidlar odatda biznesingizni tavsiflaganingizdan keyin bir necha kun ichida keladi, soʻng haftama-hafta davom etadi. Free tarifidan boshlang: bitta kampaniya va oyiga oʻnta saralangan lid — bir dollar sarflamasdan farqni sezish uchun yetarli.',
  'faq.6.q': 'Istalgan vaqtda bekor qila olamanmi?',
  'faq.6.a':
    'Ha. Oylik tariflar bir bosishda bekor qilinadi va toʻlangan davr oxirida kuchga kiradi. Shartnoma ham, «ushlab qolish qoʻngʻiroqlari» ham yoʻq. Free tarifi uchun karta umuman kerak emas.',

  /* ---- footer ---- */
  'footer.tagline.html':
    'AIZU 合図 — bu harakatga chorlovchi signal,<br>tekshirib turadigan yana bir<br>platforma emas.',
  'footer.nav': 'Saytning pastki qismi',
  'footer.col.product': 'Mahsulot',
  'footer.col.features': 'Imkoniyatlar',
  'footer.col.pricing': 'Narxlar',
  'footer.col.resources': 'Resurslar',
  'footer.link.why': 'Nega AIZU',
  'footer.link.how': 'Qanday ishlaydi',
  'footer.link.pricing': 'Narxlar',
  'footer.link.faq': 'FAQ',
  'footer.link.docs': 'Hujjatlar',
  'footer.link.privacy': 'Maxfiylik',
  'footer.link.terms': 'Shartlar',
  'footer.social': 'Bizni kuzating'
};
