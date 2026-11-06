# EzCompressor

Windows için basit bir sidechain ducker. Seçilen kaynağın (mikrofon, çıkış cihazı ya da uygulama) ses seviyesine göre başka bir kanalın Windows ses seviyesini kompresör gibi kısar.

## Özellikler

- **Kaynak (sidechain):** giriş aygıtı, çıkış aygıtı veya çalışan bir uygulama (ör. Spotify, Discord)
- **Hedef:** çıkış aygıtı (ör. Wave Link kanalları) veya uygulama
- **Ayarlar:** Threshold, Hassasiyet, Ratio, Attack, Release, Max kısma, Hedef seviyesi
- **Statik kısma modu:** kaynak threshold'u geçtiğinde hedef sabit bir miktarda kısılır
- **Hassasiyet:** %100'de anlık tepelere, %0'da yaklaşık 1 saniyelik ortalama seviyeye tepki verir
- ON/OFF düğmesi, canlı giriş ve kısma göstergeleri
- Sistem tepsisinde arka planda çalışır, tek kopya çalışabilir
- Ayarlar exe'nin yanındaki `EzCompressor.json` dosyasına kaydedilir

## Kullanım

[Releases](../../releases) sayfasından `EzCompressor.exe` dosyasını indirip çalıştırın. Python gerekmez.

Pencereyi kapatınca uygulama tepside çalışmaya devam eder. Tamamen kapatmak için tepsi simgesine sağ tıklayıp **Çıkış** seçin.

Windows açılışında başlatmak için `shell:startup` klasörüne exe'nin kısayolunu koyun. Kısayol hedefine `--hidden` eklerseniz pencere açılmadan tepside başlar.

## Kaynaktan çalıştırma

```bash
pip install -r requirements.txt
python ezcompressor.py
```

## Exe oluşturma

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name EzCompressor --icon icon.ico --hidden-import pystray._win32 ezcompressor.py
```
