# Broute Collector

تجمیع‌کننده رایگان و عمومی کانفیگ‌های VPN — منتشرشده روی GitHub Pages.

> ⚠️ این پروژه یک **سرویس VPN اختصاصی نیست**. این پروژه صرفاً کانفیگ‌های رایگان و
> عمومی منتشرشده در مخازن GitHub، فایل‌های متنی عمومی و منابع باز دیگر را
> جمع‌آوری، پاک‌سازی، دسته‌بندی و در قالب یک لینک Subscription واحد منتشر می‌کند.
> این پروژه مالک هیچ‌یک از سرورها نیست و هیچ تضمینی درباره امنیت، پایداری،
> سرعت یا سیاست لاگ آن‌ها نمی‌دهد.

## هدف پروژه

- جمع‌آوری خودکار کانفیگ‌های عمومی VPN از منابع مجاز (Allowlist)
- پاک‌سازی، اعتبارسنجی ساختاری و حذف موارد تکراری یا ناقص
- بررسی اولیه زنده‌بودن سرورها (DNS + TCP Port Check)
- انتشار خروجی در قالب فایل‌های Subscription سازگار با کلاینت‌های محبوب
- نمایش وضعیت در یک وب‌سایت استاتیک روی GitHub Pages

## لینک Subscription

لینک‌های تنظیم‌شده برای این مخزن:

```
https://xbroute.github.io/Broute-Collector/data/sub.txt
https://xbroute.github.io/Broute-Collector/data/sub-base64.txt
https://xbroute.github.io/Broute-Collector/data/online-sub.txt
https://xbroute.github.io/Broute-Collector/data/online-sub-base64.txt
```

خروجی‌های تفکیک‌شده بر اساس پروتکل نیز در `data/vless.txt`، `data/vmess.txt`،
`data/trojan.txt`، `data/shadowsocks.txt` و `data/hysteria2.txt` موجود است.
فایل `data/secure.txt` فقط کانفیگ‌های دارای TLS/Reality را شامل می‌شود.
این برچسب وجود TLS/Reality در تنظیمات را نشان می‌دهد و تضمین اعتماد به سرور نیست.
خروجی `online-sub` فقط رکوردهای معتبر با آخرین وضعیت آنلاین را دارد؛ خروجی
معمولی تا پیش از سه شکست متوالی، رکوردهای آفلاین را نیز نگه می‌دارد.
رکوردهای بررسی‌نشده، حذف‌شده یا متعلق به منبع موقتاً قطع‌شده منتشر نمی‌شوند.

## ساختار پروژه

```
project/
├── index.html                 صفحه اصلی وب‌سایت
├── assets/                    استایل، اسکریپت و لوگو
├── data/                      فایل‌های خروجی و تنظیمات (منابع، برند، وضعیت)
├── scripts/                   پایپ‌لاین پایتون (collector → parser → deduplicator → validator → generator)
├── tests/                     تست‌های پایتون و مرورگر
├── docs/                      گزارش بررسی و محدودیت‌های عملیاتی
├── .github/workflows/         اجرای خودکار GitHub Actions
├── requirements.txt
└── LICENSE
```

## اجرای محلی

پایتون ۳٫۱۱ یا بالاتر لازم است. دستورات زیر را از ریشه مخزن اجرا کنید:

```bash
git clone https://github.com/xbroute/Broute-Collector.git
cd Broute-Collector
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python scripts/generator.py
python scripts/generate_online_subscription.py
```

اجرای generator فایل‌های `data/` را بازنویسی می‌کند و به منابع و پورت سرورها
درخواست شبکه می‌فرستد. برای اجرای کامل همراه منابع رمزگذاری‌شده بات، از
`python scripts/generator_managed.py` استفاده کنید؛ متغیرهای کلید و مسیر state
باید مثل Workflow تنظیم شده باشند.

برای پیش‌نمایش سایت به‌صورت محلی، یک سرور استاتیک ساده کافی است:

```bash
python -m http.server 8000
# سپس مرورگر را به http://localhost:8000 باز کنید
```

## نحوه افزودن منبع جدید

فایل `data/sources.json` را ویرایش کنید. فقط منابعی که `enabled: true` دارند
دریافت می‌شوند — از افزودن خودکار منابع ناشناس جلوگیری می‌شود.

```json
{
  "github_sources": [
    {
      "name": "نام منبع",
      "url": "https://raw.githubusercontent.com/user/repo/main/sub.txt",
      "enabled": true
    }
  ]
}
```

`telegram_sources` کانال‌های عمومی را از صفحه پیش‌نمایش تلگرام دریافت می‌کند.
`manual_sources` برای فایل‌های محلی و `subscription_sources` برای لینک‌های
Subscription است. تعداد دریافت‌های هم‌زمان با `settings.fetch_workers` تنظیم
می‌شود؛ پیش‌فرض ۵ و بازه مجاز ۱ تا ۱۰ است.

خطای دریافت منبع از پاسخ موفقِ خالی تفکیک می‌شود. هنگام خطای موقت، سابقه
کانفیگ‌های آن منبع حداکثر ۳۰ دقیقه با وضعیت بررسی‌نشده برای بازیابی حفظ می‌شود
و در خروجی عمومی قرار نمی‌گیرد. منبعی که غیرفعال یا حذف شده یا پاسخ موفقِ
خالی داده است، این مهلت را ندارد. `SOURCE_FAILURE_GRACE_SECONDS=0` این قابلیت
را خاموش می‌کند؛ مقدار پیش‌فرض ۱۸۰۰ ثانیه است.

`data/status.json` آمار دریافت موفق/ناموفق را در `collection` و آمار بودجه و
خطاهای اعتبارسنجی را در `validation` دارد. `MAX_VALIDATIONS_PER_RUN` سقف تست هر
اجرا را تعیین می‌کند؛ پیش‌فرض ۵۰۰ است و نوبت تست بین منابع تقسیم می‌شود.

## نحوه تغییر مشخصات برند

فایل `data/brand.json` را ویرایش کنید:

```json
{
  "brand_name": "نام سرویس شما",
  "short_name": "VPN",
  "description": "توضیح کوتاه",
  "telegram": "https://t.me/your_channel",
  "github": "https://github.com/your-username/your-repo",
  "logo": "assets/logo.png",
  "primary_color": "#7c3aed"
}
```

لوگوی خودتان را جایگزین `assets/logo.png` کنید.

## فعال‌سازی GitHub Pages

1. به تنظیمات مخزن بروید: **Settings → Pages**
2. در بخش **Build and deployment**، گزینه **Source** را روی **GitHub Actions** قرار دهید
3. پس از اولین اجرای موفق Workflow، آدرس سایت در همان صفحه نمایش داده می‌شود

## فعال‌سازی GitHub Actions

Workflow جمع‌آوری برای اجرای هر ۵ دقیقه زمان‌بندی شده است:

```yaml
schedule:
  - cron: "3-58/5 * * * *"
```

برای اجرای دستی: به تب **Actions** بروید، Workflow با نام
**Update Subscription** را انتخاب و دکمه **Run workflow** را بزنید.

اگر مخزن Private است یا Actions غیرفعال است، از **Settings → Actions →
General** آن را فعال کنید و مطمئن شوید دسترسی **Read and write permissions**
برای `GITHUB_TOKEN` فعال باشد (لازم برای Commit خودکار).

زمان‌بندی Actions تضمین اجرای دقیق در هر پنج دقیقه نیست؛ زمان آخرین اجرا در
سایت و `status.json` نمایش داده می‌شود. ناشر تلگرام و کنترل بات Workflow و
state جداگانه دارند.

## مدیریت تلگرام

در پیام خصوصی بات `/admin` یا `/start` را بفرستید. پنل نقش‌های مالک، مدیر، اپراتور
و مشاهده‌گر، مقصدهای صفحه‌بندی‌شده، مدیریت منابع، گزارش سلامت، audit و پشتیبان
رمزگذاری‌شده دارد. `telegram_bot_control_full.py` همچنان تنها مصرف‌کنندهٔ
`getUpdates` است؛ صف و تاریخچهٔ هر مقصد جدا می‌ماند.

مدیران قبلی به مالک مهاجرت می‌کنند. در راه‌اندازی تازه، مالک را با Secret
`TELEGRAM_OWNER_USER_IDS` معرفی کنید؛ در نبود آن، فقط ادمین تأییدشدهٔ گروه کنترل
اصلی می‌تواند مالک اولیه شود. ادمین‌کردن بات در گروه دلخواه مدیریت سراسری نمی‌دهد.
مقصد تازه خاموش ثبت می‌شود؛ پیش از فعال‌کردن، مجوز واقعی انتشار بررسی می‌شود.

| دستور | کاربرد |
|---|---|
| `/admin`، `/help`، `/whoami` | پنل، راهنمای کامل و شناسه/نقش حساب |
| `/admins`، `/admin_add 123456 operator` | مشاهده و مدیریت تیم؛ تغییر با تأیید مالک |
| `/publisher_on`، `/publisher_off`، `/publisher_status` | کنترل سراسری انتشار |
| `/publisher_pause 1h`، `/publisher_resume` | توقف موقت کلی |
| `/targets` | فهرست مقصدها و تنظیمات هرکدام |
| `/target_on 1`، `/target_off 1` | روشن/خاموش‌کردن مقصد |
| `/target_interval 1 30-90` | فاصله تصادفی ارسال؛ حداقل ۱۵ ثانیه |
| `/target_template 1` سپس خط جدید و قالب | قالب پیام با دقیقاً یک `{config}` |
| `/target_topic 1 123` | Topic مقصد؛ صفر برای حذف Topic |
| `/target_filter 1 protocol=vless country=DE tls=on latency=250` | فیلتر واقعی صف و ارسال |
| `/target_schedule 1 09:00-23:00 UTC+03:30` | ساعات ارسال؛ `off` بدون محدودیت |
| `/target_quota 1 100` | سهمیهٔ روزانه؛ صفر نامحدود |
| `/target_pause 1 2h`، `/target_resume 1` | توقف موقت مقصد |
| `/target_preview 1`، `/target_check 1` | پیش‌نمایش خصوصی و بررسی مجوز |
| `/target_rollback 1`، `/target_remove 1` | بازگشت تنظیمات/حذف با تأیید |
| `/queue 1`، `/queue_retry 1`، `/queue_rebuild 1` | وضعیت و بازیابی صف با حفظ تاریخچه |
| `/queue_resolve 1 <id> sent` یا `retry` | تعیین تکلیف ارسال مبهم با تأیید |
| `/source_add https://example.com/sub` | افزودن منبع با کنترل آدرس و محتوا |
| `/sources`، `/source_on 1`، `/source_off 1`، `/source_check 1` | نمایش، کنترل و probe منابع |
| `/source_name 1 نام`، `/source_remove 1` | نام‌گذاری و حذف تأییدشده |
| `/buy_button`، `/buy_link`، `/buy_text` | مدیریت دکمه و متن خرید |
| `/report`، `/health`، `/audit` | گزارش، سلامت و سابقهٔ تغییرات |
| `/backup`، `/restore`، `/cancel` | پشتیبان، بازیابی مالک و لغو درخواست |

توکن و کلیدها را فقط در **Settings → Secrets and variables → Actions** ذخیره کنید:

| Secret | استفاده |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot API؛ کلید سازگار اولیه برای state رمزگذاری‌شده |
| `TELEGRAM_SOURCE_ENCRYPTION_KEY` | کلید مستقل منابع مدیریتی |
| `TELEGRAM_DESTINATION_ENCRYPTION_KEY` | کلید مستقل مقصدها |
| `TELEGRAM_OWNER_USER_IDS` | شناسهٔ عددی مالک/مالکان، جداشده با کاما یا فاصله؛ مسیر بازیابی دسترسی |

فایل‌های رمزگذاری‌شده روی شاخه `telegram-bot-state` و سابقه عملیاتی ناشر روی
`telegram-state` قرار دارند. افزودن کلید مستقل منابع، state قدیمی رمزگذاری‌شده
با توکن فعلی را قابل خواندن نگه می‌دارد؛ برای تعویض هم‌زمان کلید و توکن، ابتدا
state را با کلید جدید بازنویسی کنید. state خراب باعث توقف امن پردازش می‌شود.

پیش از ارسال، قصد ارسال checkpoint می‌شود؛ پاسخ مبهم یا وقفه قبل از ثبت موفقیت
برای بررسی نگه داشته می‌شود و خودکار تکرار نمی‌شود. بازیابی تنظیمات با انتشار
خاموش انجام می‌شود و تاریخچهٔ ارسال را پاک نمی‌کند. GitHub Actions پاسخ آنی و
SLA تجاری تضمین نمی‌کند. جزئیات کامل، حدود امکانات و دستورها در
[راهنمای مدیریت](docs/TELEGRAM_ADMINISTRATION.md) آمده است.

## تست و دیباگ

تست‌های پایتون بدون توکن و بدون ارسال واقعی تلگرام اجرا می‌شوند:

```bash
python -m compileall -q scripts tests
python -m unittest discover -s tests -v
```

برای تست تعامل سایت، Node.js ۲۲ یا بالاتر و Chromium لازم است. وابستگی Node
فقط برای توسعه و CI است؛ سایت همچنان استاتیک و بدون مرحله build کار می‌کند.

```bash
npm ci
npx playwright install --with-deps chromium
npm run test:browser
```

تست مرورگر یک سرور موقت روی localhost می‌سازد، داده مصنوعی تزریق می‌کند و
درخواست‌های خارجی را می‌بندد. `BROUTE_BROWSER_EXECUTABLE` مسیر مرورگر نصب‌شده و
`BROUTE_SCREENSHOT_DIR` مسیر ذخیره تصاویر بررسی را مشخص می‌کند. Workflow
**Collector Regression Tests** کل مجموعه را روی Pull Request اجرا می‌کند.

گزارش یافته‌ها، تست‌ها و پیشنهادهای بعدی در
[docs/RELIABILITY_AUDIT.md](docs/RELIABILITY_AUDIT.md) است.

## توضیح خط لوله پردازش

| مرحله | فایل | وظیفه |
|---|---|---|
| ۱ | `scripts/collector.py` | دریافت محتوای خام از منابع مجاز |
| ۲ | `scripts/parser.py` | استخراج خطوط کانفیگ، تشخیص Base64، تشخیص پروتکل |
| ۳ | `scripts/deduplicator.py` | حذف کانفیگ‌های تکراری بر اساس شناسه اتصال |
| ۴ | `scripts/validator.py` | بررسی DNS و TCP Port، تشخیص تقریبی کشور |
| ۵ | `scripts/generator.py` | تولید تمام فایل‌های خروجی و `status.json` |

«آنلاین» فقط نتیجه آخرین DNS/TCP Port Check است؛ احراز هویت، TLS handshake،
اتصال واقعی تونل، سرعت و دسترسی از شبکه کاربر تست نمی‌شود. Hysteria2، TUIC و
WireGuard از UDP استفاده می‌کنند و این تست TCP وضعیت واقعی آن‌ها را اثبات
نمی‌کند. کشور نیز تخمین GeoIP است. گزارش بررسی مسیر پیشنهادی برای تست واقعی
پروتکل‌ها و بهبود کش GeoIP را توضیح می‌دهد.

## هشدار امنیتی

این پروژه صرفاً کانفیگ‌های رایگان و عمومی منتشرشده در منابع مختلف را
جمع‌آوری و دسته‌بندی می‌کند. سرورها متعلق به این پروژه نیستند و امنیت،
پایداری، سرعت، حریم خصوصی یا سیاست نگهداری لاگ آن‌ها تضمین نمی‌شود. از این
کانفیگ‌ها برای بانکداری، کیف پول رمزارز، صرافی، ایمیل اصلی، اطلاعات کاری یا
انتقال داده‌های حساس استفاده نکنید. IP کاربر ممکن است توسط اپراتور سرور ثبت
شود و استفاده از پروژه کاملاً بر عهده کاربر است.

## قوانین مشارکت

- Pull Request ها باید فقط شامل تغییرات کد/مستندات باشند، نه افزودن مستقیم
  منبع ناشناس بدون بررسی
- کد جدید باید خطاها را مدیریت کند و اطلاعات حساس را در Log چاپ نکند
- منطق اصلی جمع‌آوری از کتابخانه استاندارد پایتون استفاده می‌کند؛ `cryptography`
  برای state رمزگذاری‌شده بات و Playwright فقط برای تست مرورگر است
- هرگونه تغییر در فیلترهای امنیتی باید در توضیح PR ذکر شود

## مجوز

این پروژه تحت مجوز MIT منتشر شده است — فایل [LICENSE](LICENSE) را ببینید.
