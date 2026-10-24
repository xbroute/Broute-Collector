Offline dependencies, preserving the existing QR API and font family:

- qrcodejs 1.0.0 from npm (`qrcodejs-1.0.0.tgz`), MIT; upstream https://github.com/davidshimjs/qrcodejs
- Vazirmatn 33.0.3 from npm (`vazirmatn-33.0.3.tgz`), SIL OFL 1.1; upstream https://github.com/rastikerdar/vazirmatn

Licenses accompany the files. `checksums.json` records SHA-256 hashes. The QR
regression decodes the generated image to verify its actual subscription URL;
all external browser requests remain disabled in that test.
