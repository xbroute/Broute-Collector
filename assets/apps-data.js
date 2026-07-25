/* apps-data.js
 * فهرست برنامه‌های کلاینت به‌تفکیک سیستم‌عامل، همراه با لینک دانلود رسمی و
 * آموزش افزودن لینک Subscription داخل هرکدوم.
 * این فایل جدا از script.js نگه داشته شده تا آپدیت‌کردن لیست برنامه‌ها ساده باشه.
 */
window.PLATFORM_APPS = {
  android: {
    label: "اندروید",
    emoji: "🤖",
    apps: [
      {
        name: "Hiddify",
        emoji: "🔴",
        download: "https://github.com/hiddify/hiddify-app/releases/latest",
        steps: [
          "لینک Subscription رو از بالای صفحه کپی کن.",
          "برنامه‌ی Hiddify رو باز کن.",
          "روی دکمه‌ی + (افزودن پروفایل) بزن.",
          "گزینه‌ی «Add from Clipboard» یا «افزودن از کلیپ‌بورد» رو انتخاب کن.",
          "پروفایل جدید اضافه می‌شه؛ روش بزن و دکمه‌ی اتصال رو فعال کن."
        ]
      },
      {
        name: "NapsternetV (Npv Tunnel)",
        emoji: "🟢",
        download: "https://play.google.com/store/apps/details?id=com.napsternetlabs.napsternetv",
        steps: [
          "لینک Subscription رو کپی کن.",
          "برنامه رو باز کن، روی + بزن.",
          "گزینه‌ی Import یا V2ray Subscription رو انتخاب کن.",
          "لینک رو Paste کن و Save بزن.",
          "روی پروفایل ساخته‌شده بزن و اتصال رو برقرار کن."
        ]
      },
      {
        name: "V2Box",
        emoji: "🟠",
        download: "https://play.google.com/store/apps/details?id=dev.hexasoftware.v2box",
        steps: [
          "لینک Subscription رو کپی کن.",
          "برنامه‌ی V2Box رو باز کن.",
          "به بخش Subscribe یا + بزن.",
          "گزینه‌ی Import from Clipboard رو بزن.",
          "بعد از افزوده‌شدن، سرور موردنظر رو انتخاب و وصل شو."
        ]
      },
      {
        name: "V2RayNG",
        emoji: "🔵",
        download: "https://play.google.com/store/apps/details?id=com.v2ray.ang",
        steps: [
          "لینک Subscription رو کپی کن.",
          "برنامه‌ی v2rayNG رو باز کن.",
          "از منوی سه‌خط بالا سمت چپ، Subscription group setting رو باز کن.",
          "با علامت + یک گروه جدید بساز و لینک رو Paste کن.",
          "برگرد و روی آیکون Update (بازخوانی) بزن تا سرورها لود بشن."
        ]
      },
      {
        name: "OneClick",
        emoji: "🔵",
        download: "https://play.google.com/store/search?q=oneclick%20v2ray",
        steps: [
          "لینک Subscription رو کپی کن.",
          "برنامه رو باز کن و روی + بزن.",
          "گزینه‌ی افزودن از لینک/کلیپ‌بورد رو انتخاب کن.",
          "لینک رو وارد و ذخیره کن.",
          "روی سرور موردنظر بزن و متصل شو."
        ]
      },
      {
        name: "Happ",
        emoji: "🔴",
        download: "https://play.google.com/store/apps/details?id=com.happproxy",
        steps: [
          "لینک Subscription رو کپی کن.",
          "برنامه‌ی Happ رو باز کن.",
          "روی + پایین صفحه بزن.",
          "گزینه‌ی افزودن از کلیپ‌بورد/URL رو انتخاب کن.",
          "بعد از افزوده‌شدن سرورها، یکی رو انتخاب و وصل شو."
        ]
      }
    ]
  },

  ios: {
    label: "آیفون",
    emoji: "📱",
    apps: [
      {
        name: "Hiddify",
        emoji: "🔴",
        download: "https://hiddifynext.app/en/guides/",
        steps: [
          "چون Hiddify هنوز رسماً روی App Store نیست، طبق راهنمای سایت رسمی از طریق TestFlight نصبش کن.",
          "لینک Subscription رو کپی کن.",
          "توی برنامه، روی + بزن و «Add from Clipboard» رو انتخاب کن.",
          "پروفایل اضافه می‌شه؛ وصل شو."
        ]
      },
      {
        name: "NapsternetV (Npv Tunnel)",
        emoji: "🟢",
        download: "https://napsternetv.vonmatrix.com/",
        steps: [
          "از سایت رسمی، لینک App Store رو باز کن و نصب کن.",
          "لینک Subscription رو کپی کن.",
          "توی برنامه + بزن و Import از کلیپ‌بورد رو انتخاب کن.",
          "سرور رو انتخاب و وصل شو."
        ]
      },
      {
        name: "V2Box",
        emoji: "🟠",
        download: "https://apps.apple.com/us/app/v2box-v2ray-client/id6446814690",
        steps: [
          "لینک Subscription رو کپی کن.",
          "برنامه رو باز کن، بخش Subscribe یا + رو بزن.",
          "Import from Clipboard رو انتخاب کن.",
          "سرور رو انتخاب و وصل شو."
        ]
      },
      {
        name: "V2rayTun",
        emoji: "🔵",
        download: "https://apps.apple.com/us/app/v2raytun/id6476628951",
        steps: [
          "لینک Subscription رو کپی کن.",
          "برنامه رو باز کن و روی + بزن.",
          "گزینه‌ی Import from Clipboard یا Deep Link رو انتخاب کن.",
          "سرور رو انتخاب و وصل شو."
        ]
      },
      {
        name: "Streisand",
        emoji: "🔵",
        download: "https://apps.apple.com/us/app/streisand/id6450534064",
        steps: [
          "لینک Subscription رو کپی کن.",
          "برنامه رو باز کن، روی + بزن.",
          "گزینه‌ی «Add from Clipboard» رو انتخاب کن.",
          "روی سرور موردنظر لمس طولانی کن و Latency رو بزن تا سرعت‌ها چک بشن، بعد وصل شو."
        ]
      },
      {
        name: "FoxRay",
        emoji: "🔴",
        download: "https://foxray.org/",
        steps: [
          "توجه: FoxRay ممکنه موقتاً روی App Store در دسترس نباشه؛ طبق سایت رسمی از نسخه‌ی جایگزین یا V2rayTun استفاده کن.",
          "لینک Subscription رو کپی کن.",
          "توی برنامه، دکمه‌ی افزودن کانفیگ رو بزن و Paste کن.",
          "روی سرور موردنظر بزن و وصل شو."
        ]
      },
      {
        name: "Karing",
        emoji: "🟣",
        download: "https://apps.apple.com/us/app/karing/id6472431552",
        steps: [
          "لینک Subscription رو کپی کن.",
          "برنامه‌ی Karing رو باز کن.",
          "روی + بزن و گزینه‌ی افزودن از کلیپ‌بورد/URL رو انتخاب کن.",
          "سرور رو انتخاب و وصل شو."
        ]
      },
      {
        name: "Happ",
        emoji: "🔴",
        download: "https://apps.apple.com/us/search?term=Happ%20Proxy",
        steps: [
          "لینک Subscription رو کپی کن.",
          "برنامه‌ی Happ رو باز کن.",
          "روی + بزن و افزودن از کلیپ‌بورد/URL رو انتخاب کن.",
          "سرور رو انتخاب و وصل شو."
        ]
      }
    ]
  },

  desktop: {
    label: "ویندوز/مک",
    emoji: "💻",
    apps: [
      {
        name: "Hiddify",
        emoji: "🔴",
        download: "https://github.com/hiddify/hiddify-app/releases/latest",
        steps: [
          "لینک Subscription رو کپی کن.",
          "برنامه‌ی Hiddify رو نصب و باز کن.",
          "روی + بزن و «Add from Clipboard» رو انتخاب کن.",
          "پروفایل اضافه می‌شه؛ روی دکمه‌ی اتصال بزن."
        ]
      },
      {
        name: "v2rayN",
        emoji: "🟢",
        download: "https://github.com/2dust/v2rayN/releases/latest",
        steps: [
          "لینک Subscription رو کپی کن.",
          "برنامه‌ی v2rayN رو باز کن.",
          "از منوی Subscriptions، گزینه‌ی Add رو بزن و لینک رو Paste کن.",
          "روی Update Subscriptions بزن تا سرورها لود بشن، بعد یکی رو انتخاب و System Proxy رو فعال کن."
        ]
      },
      {
        name: "Happ",
        emoji: "🟠",
        download: "https://play.google.com/store/apps/details?id=com.happproxy",
        steps: [
          "توجه: Happ عمدتاً موبایل‌محوره؛ اگه نسخه‌ی دسکتاپ در دسترس نبود از v2rayN یا Nekoray استفاده کن.",
          "لینک Subscription رو کپی کن و طبق راهنمای داخل برنامه اضافه کن."
        ]
      },
      {
        name: "Koray VPN",
        emoji: "🔵",
        download: "https://github.com/2dust/v2rayN/releases/latest",
        steps: [
          "این برنامه به‌صورت رسمی و قابل‌تأیید پیدا نشد؛ به‌جاش پیشنهاد می‌کنیم از v2rayN (بالا) استفاده کنی که همون کارکرد رو داره.",
          "لینک Subscription رو کپی و طبق آموزش v2rayN اضافه کن."
        ]
      },
      {
        name: "Nekoray VPN",
        emoji: "🔵",
        download: "https://github.com/MatsuriDayo/nekoray/releases/latest",
        steps: [
          "توجه: پروژه‌ی اصلی Nekoray دیگه به‌روزرسانی نمی‌شه؛ همچنان قابل‌استفاده‌ست ولی اگه مشکلی دیدی v2rayN رو امتحان کن.",
          "لینک Subscription رو کپی کن.",
          "برنامه رو باز کن، از منوی Program یا Group، گزینه‌ی Add profile from clipboard رو بزن.",
          "روی Update Subscription بزن و بعد سرور رو انتخاب و وصل شو."
        ]
      }
    ]
  }
};