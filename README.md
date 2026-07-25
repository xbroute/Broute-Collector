# یام‌یام پروکسی

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

پس از فعال‌سازی GitHub Pages، لینک‌های زیر در دسترس خواهند بود:

```
https://YOUR_GITHUB_USERNAME.github.io/YOUR_REPOSITORY_NAME/data/sub.txt
https://YOUR_GITHUB_USERNAME.github.io/YOUR_REPOSITORY_NAME/data/sub-base64.txt
```

خروجی‌های تفکیک‌شده بر اساس پروتکل نیز در `data/vless.txt`، `data/vmess.txt`،
`data/trojan.txt`، `data/shadowsocks.txt` و `data/hysteria2.txt` موجود است.
فایل `data/secure.txt` فقط کانفیگ‌های دارای TLS/Reality را شامل می‌شود.

## ساختار پروژه

```
project/
├── index.html                 صفحه اصلی وب‌سایت
├── assets/                    استایل، اسکریپت و لوگو
├── data/                      فایل‌های خروجی و تنظیمات (منابع، برند، وضعیت)
├── scripts/                   پایپ‌لاین پایتون (collector → parser → deduplicator → validator → generator)
├── .github/workflows/         اجرای خودکار GitHub Actions
├── requirements.txt
└── LICENSE
```

## اجرای محلی

```bash
git clone https://github.com/YOUR_GITHUB_USERNAME/YOUR_REPOSITORY_NAME.git
cd YOUR_REPOSITORY_NAME
pip install -r requirements.txt

cd scripts
python generator.py     # کل پایپ‌لاین را اجرا می‌کند و data/ را به‌روزرسانی می‌کند
```

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

منابع تلگرام در نسخه اولیه به‌صورت اختیاری و از طریق فایل واسط دستی
(`data/manual.txt`) پشتیبانی می‌شوند. توکن‌های Bot API (در صورت افزودن در
آینده) باید فقط در **GitHub Secrets** ذخیره شوند، هرگز داخل کد.

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

Workflow به‌صورت پیش‌فرض هر ۶ ساعت اجرا می‌شود:

```yaml
schedule:
  - cron: "0 */6 * * *"
```

برای اجرای دستی: به تب **Actions** بروید، Workflow با نام
**Update Subscription** را انتخاب و دکمه **Run workflow** را بزنید.

اگر مخزن Private است یا Actions غیرفعال است، از **Settings → Actions →
General** آن را فعال کنید و مطمئن شوید دسترسی **Read and write permissions**
برای `GITHUB_TOKEN` فعال باشد (لازم برای Commit خودکار).

## توضیح خط لوله پردازش

| مرحله | فایل | وظیفه |
|---|---|---|
| ۱ | `scripts/collector.py` | دریافت محتوای خام از منابع مجاز |
| ۲ | `scripts/parser.py` | استخراج خطوط کانفیگ، تشخیص Base64، تشخیص پروتکل |
| ۳ | `scripts/deduplicator.py` | حذف کانفیگ‌های تکراری بر اساس شناسه اتصال |
| ۴ | `scripts/validator.py` | بررسی DNS و TCP Port، تشخیص تقریبی کشور |
| ۵ | `scripts/generator.py` | تولید تمام فایل‌های خروجی و `status.json` |

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
- از افزودن وابستگی غیرضروری خودداری کنید (پروژه عمداً بدون وابستگی خارجی است)
- هرگونه تغییر در فیلترهای امنیتی باید در توضیح PR ذکر شود

## مجوز

این پروژه تحت مجوز MIT منتشر شده است — فایل [LICENSE](LICENSE) را ببینید.
