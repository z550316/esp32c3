#include <Arduino.h>
#include "driver/i2s_std.h"
#include "driver/gpio.h"
#include <WiFi.h>
#include <WebServer.h>
#include <Preferences.h>

#define I2S_WS      6
SET_LOOP_TASK_STACK_SIZE(16384);
#define I2S_BCLK    8
#define I2S_DOUT    5
#define I2S_DIN     7
#define I2S_MCLK    10
#define I2C_SDA     3
#define I2C_SCL     4
#define PA_ENABLE   11
#define LED_PIN     12
#define BOOT_BTN    9

#define ES8311_ADDR 0x18
#define SAMPLE_RATE 16000

#define FRAME_HEADER1 0xAA
#define FRAME_HEADER2 0x55

#define CMD_REC_START   0x01
#define CMD_REC_STOP    0x02
#define CMD_PLAY_AUDIO  0x04
#define CMD_PLAY_TONE   0x06
#define CMD_PLAY_MELODY 0x07
#define CMD_SET_VOLUME  0x0A
#define CMD_SET_WIFI    0x0B
#define CMD_WIFI_SCAN   0x0C
#define CMD_WIFI_SSIDS  0x0D

#define CMD_REC_DATA    0x01
#define CMD_REC_STARTED 0x03
#define CMD_PLAY_DONE   0x05

#define TCP_PORT        11348
#define STA_TIMEOUT_MS  15000

#define RING_BUF_SIZE (128 * 1024)
static uint8_t ringBuf[RING_BUF_SIZE];
static volatile uint32_t ringHead = 0;
static volatile uint32_t ringTail = 0;
static volatile bool ringEos = false;
static volatile bool playRequest = false;
static volatile uint32_t playTotal = 0;
static volatile bool playActive = false;
static volatile bool playDonePending = false;

volatile int currentState = 0;
unsigned long stateStart = 0;
unsigned long bootMillis = 0;
#define REC_TIMEOUT_MS 120000
#define PLAY_TIMEOUT_MS 30000
#define BOOT_GUARD_MS 5000
#define REC_BUF_SIZE 2048
uint8_t recBuffer[REC_BUF_SIZE];

i2s_chan_handle_t tx_chan = NULL;
i2s_chan_handle_t rx_chan = NULL;

Preferences nvs;
WiFiServer tcpServer(TCP_PORT);
WiFiClient tcpClient;
bool wifiConnected = false;
bool tcpClientConnected = false;
bool useWifi = false;

#define AP_SSID_PREFIX "Xiaozhi-"
WebServer apServer(80);
bool apMode = false;

volatile bool btnPressed = false;
volatile bool btnReleased = false;

void IRAM_ATTR btnISR(void* arg) {
    uint32_t level = gpio_get_level((gpio_num_t)BOOT_BTN);
    if (level == 0) {
        btnPressed = true;
    } else {
        btnReleased = true;
    }
}

void startRec();
void stopRec(bool sendStop = true);

enum { S_SYNC0, S_SYNC1, S_HDR, S_DATA };

struct FrameParser {
    volatile int fsm = S_SYNC0;
    uint8_t hdr[6];
    uint32_t fLen = 0;
    uint8_t fCmd = 0;
    int fPos = 0;
    uint8_t batchBuf[1024];
    int batchLen = 0;
    uint8_t wifiBuf[256];
    int wifiPos = 0;

    void flushBatch() {
        if (batchLen > 0) {
            ringWrite(batchBuf, batchLen);
            batchLen = 0;
        }
    }

    void processByte(uint8_t byte) {
        switch (fsm) {
            case S_SYNC0:
                if (byte == FRAME_HEADER1) fsm = S_SYNC1;
                break;
            case S_SYNC1:
                if (byte == FRAME_HEADER2) { fsm = S_HDR; fPos = 0; }
                else if (byte != FRAME_HEADER1) fsm = S_SYNC0;
                break;
            case S_HDR:
                hdr[fPos++] = byte;
                if (fPos == 5) {
                    memcpy(&fLen, hdr, 4);
                    fCmd = hdr[4];
                    if (fLen < 1 || fLen > 1048576) { fsm = S_SYNC0; break; }
                    if (fLen == 1) {
                        if (fCmd == CMD_REC_START) startRec();
                        else if (fCmd == CMD_REC_STOP) stopRec();
                        else if (fCmd == CMD_PLAY_TONE) playTone();
                        else if (fCmd == CMD_PLAY_MELODY) playMelody();
                        else if (fCmd == CMD_WIFI_SCAN) doWifiScan();
                        fsm = S_SYNC0;
                    } else {
                    if (fCmd == CMD_PLAY_AUDIO) {
                        if (currentState == 1) stopRec(false);
                        ringEos = false;
                    }
                        fPos = 0;
                        fsm = S_DATA;
                    }
                }
                break;
            case S_DATA: {
                if (fCmd == CMD_PLAY_AUDIO) {
                    while (ringFree() <= (uint32_t)(batchLen + 1)) {
                        if (!playActive) break;
                        delay(1);
                    }
                    batchBuf[batchLen++] = byte;
                    if (batchLen >= (int)sizeof(batchBuf)) {
                        ringWrite(batchBuf, batchLen);
                        batchLen = 0;
                    }
                    if (!playActive) {
                        currentState = 2;
                        playActive = true;
                        digitalWrite(PA_ENABLE, HIGH);
                    }
                } else if (fCmd == CMD_SET_WIFI) {
                    if (wifiPos < (int)sizeof(wifiBuf)) wifiBuf[wifiPos++] = byte;
                }
                fPos++;
                    if (fPos == (int)(fLen - 1)) {
                        flushBatch();
                        if (fCmd == CMD_PLAY_AUDIO) {
                            ringEos = true;
                        } else if (fCmd == CMD_SET_VOLUME) {
                            setVolume(byte);
                        } else if (fCmd == CMD_SET_WIFI && wifiPos > 1) {
                            int ssidLen = wifiBuf[0];
                            if (ssidLen > 0 && ssidLen < wifiPos) {
                                String ssid = "", password = "";
                                for (int i = 0; i < ssidLen && i < wifiPos; i++) ssid += (char)wifiBuf[1 + i];
                                for (int i = 1 + ssidLen; i < wifiPos; i++) password += (char)wifiBuf[i];
                                saveWiFiCredentials(ssid, password);
                                Serial.printf("[WIFI] saved: %s\n", ssid.c_str());
                                wifiPos = 0;
                                WiFi.disconnect(true);
                                WiFi.mode(WIFI_OFF);
                                delay(100);
                                ESP.restart();
                            }
                        }
                        wifiPos = 0;
                        fsm = S_SYNC0;
                    }
                    break;
                }
        }
    }
};

FrameParser serialParser;
FrameParser wifiParser;

static void i2c_dly() { delayMicroseconds(5); }

static void i2c_gpio_init_sda_out() {
    gpio_config_t io = {};
    io.pin_bit_mask = (1ULL << I2C_SDA);
    io.mode = GPIO_MODE_OUTPUT_OD;
    io.pull_up_en = GPIO_PULLUP_ENABLE;
    io.pull_down_en = GPIO_PULLDOWN_DISABLE;
    io.intr_type = GPIO_INTR_DISABLE;
    gpio_config(&io);
}

static void i2c_gpio_init_sda_in() {
    gpio_config_t io = {};
    io.pin_bit_mask = (1ULL << I2C_SDA);
    io.mode = GPIO_MODE_INPUT;
    io.pull_up_en = GPIO_PULLUP_ENABLE;
    io.pull_down_en = GPIO_PULLDOWN_DISABLE;
    io.intr_type = GPIO_INTR_DISABLE;
    gpio_config(&io);
}

static void i2c_gpio_init_scl_out() {
    gpio_config_t io = {};
    io.pin_bit_mask = (1ULL << I2C_SCL);
    io.mode = GPIO_MODE_OUTPUT;
    io.pull_up_en = GPIO_PULLUP_ENABLE;
    io.pull_down_en = GPIO_PULLDOWN_DISABLE;
    io.intr_type = GPIO_INTR_DISABLE;
    gpio_config(&io);
}

static void i2c_start() {
    i2c_gpio_init_sda_out();
    gpio_set_level((gpio_num_t)I2C_SDA, 1); i2c_dly();
    gpio_set_level((gpio_num_t)I2C_SCL, 1); i2c_dly();
    gpio_set_level((gpio_num_t)I2C_SDA, 0); i2c_dly();
    gpio_set_level((gpio_num_t)I2C_SCL, 0); i2c_dly();
}

static void i2c_stop() {
    i2c_gpio_init_sda_out();
    gpio_set_level((gpio_num_t)I2C_SDA, 0); i2c_dly();
    gpio_set_level((gpio_num_t)I2C_SCL, 1); i2c_dly();
    gpio_set_level((gpio_num_t)I2C_SDA, 1); i2c_dly();
    i2c_gpio_init_sda_in();
}

static void i2c_reset_bus() {
    i2c_gpio_init_scl_out();
    for (int i = 0; i < 9; i++) {
        gpio_set_level((gpio_num_t)I2C_SCL, 0); i2c_dly();
        gpio_set_level((gpio_num_t)I2C_SCL, 1); i2c_dly();
    }
    i2c_stop();
}

static bool i2c_write(uint8_t b) {
    i2c_gpio_init_sda_out();
    for (int i = 0; i < 8; i++) {
        gpio_set_level((gpio_num_t)I2C_SCL, 0); i2c_dly();
        gpio_set_level((gpio_num_t)I2C_SDA, (b & 0x80) ? 1 : 0); i2c_dly();
        gpio_set_level((gpio_num_t)I2C_SCL, 1); i2c_dly();
        b <<= 1;
    }
    gpio_set_level((gpio_num_t)I2C_SCL, 0); i2c_dly();
    i2c_gpio_init_sda_in(); i2c_dly();
    gpio_set_level((gpio_num_t)I2C_SCL, 1); i2c_dly();
    bool ack = (gpio_get_level((gpio_num_t)I2C_SDA) == 0);
    gpio_set_level((gpio_num_t)I2C_SCL, 0); i2c_dly();
    return ack;
}

static uint8_t i2c_read(bool sendAck) {
    uint8_t b = 0;
    i2c_gpio_init_sda_in();
    for (int i = 0; i < 8; i++) {
        b <<= 1;
        gpio_set_level((gpio_num_t)I2C_SCL, 0); i2c_dly();
        gpio_set_level((gpio_num_t)I2C_SCL, 1); i2c_dly();
        if (gpio_get_level((gpio_num_t)I2C_SDA)) b |= 1;
    }
    gpio_set_level((gpio_num_t)I2C_SCL, 0); i2c_dly();
    i2c_gpio_init_sda_out();
    gpio_set_level((gpio_num_t)I2C_SDA, sendAck ? 0 : 1); i2c_dly();
    gpio_set_level((gpio_num_t)I2C_SCL, 1); i2c_dly();
    gpio_set_level((gpio_num_t)I2C_SCL, 0); i2c_dly();
    i2c_gpio_init_sda_in();
    return b;
}

static bool writeES8311(uint8_t reg, uint8_t val) {
    i2c_start();
    bool ok = i2c_write(ES8311_ADDR << 1);
    if (ok) ok = i2c_write(reg);
    if (ok) ok = i2c_write(val);
    i2c_stop();
    return ok;
}

static uint8_t readES8311(uint8_t reg) {
    i2c_start();
    bool ok = i2c_write(ES8311_ADDR << 1);
    if (!ok) { i2c_stop(); return 0xFF; }
    ok = i2c_write(reg);
    if (!ok) { i2c_stop(); return 0xFF; }
    i2c_start();
    ok = i2c_write((ES8311_ADDR << 1) | 1);
    if (!ok) { i2c_stop(); return 0xFF; }
    uint8_t val = i2c_read(false);
    i2c_stop();
    return val;
}

void sendFrameRaw(uint8_t cmd, const uint8_t* data, uint32_t len) {
    uint32_t total = 1 + len;
    uint8_t hdr[7];
    hdr[0] = FRAME_HEADER1;
    hdr[1] = FRAME_HEADER2;
    hdr[2] = (uint8_t)(total & 0xFF);
    hdr[3] = (uint8_t)((total >> 8) & 0xFF);
    hdr[4] = (uint8_t)((total >> 16) & 0xFF);
    hdr[5] = (uint8_t)((total >> 24) & 0xFF);
    hdr[6] = cmd;

    if (tcpClientConnected && tcpClient.connected()) {
        tcpClient.write(hdr, 7);
        if (len > 0 && data) tcpClient.write(data, len);
    } else {
        Serial.write(hdr, 7);
        if (len > 0 && data) Serial.write(data, len);
    }
}

void initES8311() {
    writeES8311(0x44, 0x08);
    writeES8311(0x44, 0x08);

    writeES8311(0x01, 0x30);
    writeES8311(0x02, 0x00);
    writeES8311(0x03, 0x10);
    writeES8311(0x16, 0x05);
    writeES8311(0x04, 0x10);
    writeES8311(0x05, 0x00);
    writeES8311(0x0B, 0x00);
    writeES8311(0x0C, 0x00);
    writeES8311(0x10, 0x1F);
    writeES8311(0x11, 0x7F);

    writeES8311(0x0D, 0xFA);

    writeES8311(0x00, 0x80);
    delay(50);

    writeES8311(0x00, 0x80);

    writeES8311(0x01, 0x3F);

    uint8_t r06 = readES8311(0x06);
    r06 &= ~0x20;
    writeES8311(0x06, r06);

    writeES8311(0x13, 0x10);
    writeES8311(0x1B, 0x0A);
    writeES8311(0x1C, 0x6A);

    writeES8311(0x44, 0x58);

    uint8_t r09 = readES8311(0x09);
    r09 = (r09 & 0xC0) | 0x0C;
    writeES8311(0x09, r09);

    uint8_t r0A = readES8311(0x0A);
    r0A = (r0A & 0xC0) | 0x0C;
    writeES8311(0x0A, r0A);

    uint8_t r02 = readES8311(0x02) & 0x07;
    writeES8311(0x02, r02);

    writeES8311(0x05, 0x00);

    uint8_t r03 = readES8311(0x03) & 0x80;
    r03 |= 0x10;
    writeES8311(0x03, r03);

    uint8_t r04 = readES8311(0x04) & 0x80;
    r04 |= 0x10;
    writeES8311(0x04, r04);

    uint8_t r07 = readES8311(0x07) & 0xC0;
    writeES8311(0x07, r07);

    writeES8311(0x08, 0x10);

    r06 = readES8311(0x06) & 0xE0;
    r06 |= 0x03;
    writeES8311(0x06, r06);

    writeES8311(0x16, 0x05);
    writeES8311(0x17, 0xBF);
    writeES8311(0x0E, 0x02);
    writeES8311(0x12, 0x00);
    writeES8311(0x14, 0x1A);

    uint8_t r14 = readES8311(0x14) & ~0x40;
    writeES8311(0x14, r14);

    writeES8311(0x0D, 0x01);
    writeES8311(0x10, 0x1F);
    writeES8311(0x11, 0x7F);
    writeES8311(0x0B, 0x00);
    writeES8311(0x0C, 0x00);
    writeES8311(0x15, 0x40);
    writeES8311(0x37, 0x08);
    writeES8311(0x45, 0x00);
    writeES8311(0x31, 0x00);
    writeES8311(0x32, 0xA8);

    delay(30);

    Serial.printf("[ES8311] R00=%02X R01=%02X R0D=%02X R0E=%02X R09=%02X R0A=%02X\n",
        readES8311(0x00), readES8311(0x01), readES8311(0x0D),
        readES8311(0x0E), readES8311(0x09), readES8311(0x0A));
    Serial.printf("[ES8311] R08=%02X R10=%02X R11=%02X R16=%02X R17=%02X R32=%02X\n",
        readES8311(0x08), readES8311(0x10), readES8311(0x11),
        readES8311(0x16), readES8311(0x17), readES8311(0x32));
}

void initI2S() {
    if (tx_chan) {
        i2s_channel_disable(tx_chan);
        i2s_del_channel(tx_chan);
        tx_chan = NULL;
    }
    if (rx_chan) {
        i2s_channel_disable(rx_chan);
        i2s_del_channel(rx_chan);
        rx_chan = NULL;
    }

    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    chan_cfg.auto_clear = true;
    chan_cfg.dma_desc_num = 6;
    chan_cfg.dma_frame_num = 240;
    esp_err_t e = i2s_new_channel(&chan_cfg, &tx_chan, &rx_chan);
    if (e != ESP_OK) {
        Serial.printf("[I2S] new_channel failed: %d\n", e);
        return;
    }

    i2s_std_config_t std_cfg = {
        .clk_cfg = {
            .sample_rate_hz = SAMPLE_RATE,
            .clk_src = I2S_CLK_SRC_DEFAULT,
            .ext_clk_freq_hz = 0,
            .mclk_multiple = I2S_MCLK_MULTIPLE_256,
        },
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {
            .mclk = (gpio_num_t)I2S_MCLK,
            .bclk = (gpio_num_t)I2S_BCLK,
            .ws = (gpio_num_t)I2S_WS,
            .dout = (gpio_num_t)I2S_DOUT,
            .din = (gpio_num_t)I2S_DIN,
            .invert_flags = {
                .mclk_inv = false,
                .bclk_inv = false,
                .ws_inv = false,
            },
        },
    };

    e = i2s_channel_init_std_mode(tx_chan, &std_cfg);
    if (e != ESP_OK) {
        Serial.printf("[I2S] tx_init_std_mode failed: %d\n", e);
        return;
    }

    e = i2s_channel_init_std_mode(rx_chan, &std_cfg);
    if (e != ESP_OK) {
        Serial.printf("[I2S] rx_init_std_mode failed: %d\n", e);
        return;
    }

    e = i2s_channel_enable(tx_chan);
    if (e != ESP_OK) {
        Serial.printf("[I2S] tx_enable failed: %d\n", e);
        return;
    }

    e = i2s_channel_enable(rx_chan);
    if (e != ESP_OK) {
        Serial.printf("[I2S] rx_enable failed: %d\n", e);
        return;
    }

    Serial.printf("[I2S] started at %dHz (ESP-IDF v5.x std mode)\n", SAMPLE_RATE);
}

void sendFrame(uint8_t cmd, const uint8_t* data, uint32_t len) {
    sendFrameRaw(cmd, data, len);
}

void setVolume(uint8_t vol) {
    if (vol > 100) vol = 100;
    uint8_t r32 = 0x80 | (vol * 63 / 100);
    writeES8311(0x32, r32);
    sendFrame(CMD_SET_VOLUME, &vol, 1);
}

void startRec() {
    if (currentState != 0) return;
    currentState = 1;
    stateStart = millis();
    digitalWrite(LED_PIN, HIGH);
    sendFrame(CMD_REC_STARTED, NULL, 0);
}

void stopRec(bool sendStop) {
    if (currentState != 1) return;
    currentState = 0;
    digitalWrite(LED_PIN, LOW);
    if (sendStop) sendFrame(CMD_REC_STOP, NULL, 0);
}

void processRec() {
    size_t r = 0;
    esp_err_t e = i2s_channel_read(rx_chan, recBuffer, REC_BUF_SIZE, &r, 100);
    if (e == ESP_OK && r > 0) {
        sendFrame(CMD_REC_DATA, recBuffer, r);
    }
}

static uint32_t ringUsed() {
    uint32_t h = ringHead, t = ringTail;
    return (h >= t) ? (h - t) : (RING_BUF_SIZE - t + h);
}

static uint32_t ringFree() {
    return RING_BUF_SIZE - 1 - ringUsed();
}

static void ringWrite(const uint8_t* data, uint32_t len) {
    for (uint32_t i = 0; i < len; i++) {
        ringBuf[ringHead] = data[i];
        ringHead = (ringHead + 1) % RING_BUF_SIZE;
    }
}

static uint32_t ringRead(uint8_t* dst, uint32_t maxLen) {
    uint32_t avail = ringUsed();
    uint32_t toRead = (avail < maxLen) ? avail : maxLen;
    for (uint32_t i = 0; i < toRead; i++) {
        dst[i] = ringBuf[ringTail];
        ringTail = (ringTail + 1) % RING_BUF_SIZE;
    }
    return toRead;
}

static uint8_t i2sPlayBuf[4096];

void playTask(void* p) {
    (void)p;
    while (true) {
        if (!playActive) {
            vTaskDelay(1);
            continue;
        }

        digitalWrite(LED_PIN, HIGH);

        uint32_t written = 0;
        uint32_t lastDataTime = millis();
        while (playActive) {
            uint32_t avail = ringUsed();
            if (avail == 0) {
                if (ringEos) break;
                if (millis() - lastDataTime > 5000) {
                    break;
                }
                vTaskDelay(1);
                continue;
            }
            lastDataTime = millis();
            uint32_t chunk = (avail > 4096) ? 4096 : avail;
            uint32_t got = ringRead(i2sPlayBuf, chunk);
            if (got > 0) {
                size_t w = 0;
                i2s_channel_write(tx_chan, i2sPlayBuf, got, &w, 500);
                written += w;
            }
        }

        digitalWrite(LED_PIN, LOW);
        playActive = false;
        ringEos = false;
        ringHead = 0;
        ringTail = 0;
        currentState = 0;
        sendFrame(CMD_PLAY_DONE, NULL, 0);
    }
}

void playTone() {
    if (playActive) return;
    if (currentState == 1) stopRec(false);
    currentState = 2;
    digitalWrite(LED_PIN, HIGH);
    int16_t toneBuf[256];
    for (int n = 0; n < 80; n++) {
        for (int i = 0; i < 256; i++) {
            float t = (float)(n * 256 + i) / SAMPLE_RATE;
            toneBuf[i] = (int16_t)(20000.0f * sinf(2 * 3.14159f * 440 * t));
        }
        size_t w = 0;
        i2s_channel_write(tx_chan, toneBuf, 512, &w, 50);
    }
    digitalWrite(LED_PIN, LOW);
    currentState = 0;
    sendFrame(CMD_PLAY_DONE, NULL, 0);
}

void playMelody() {
    if (playActive) return;
    if (currentState == 1) stopRec(false);
    currentState = 2;
    digitalWrite(LED_PIN, HIGH);
    const float freqs[] = {262, 294, 330, 349, 392, 440, 494, 523};
    const int notes[] = {0,0,4,4,5,5,4,3,3,2,2,1,1,0};
    int16_t melBuf[256];
    for (int n = 0; n < 14; n++) {
        float f = freqs[notes[n]];
        int idx = 0;
        for (int i = 0; i < SAMPLE_RATE / 4; i++) {
            melBuf[idx++] = (int16_t)(20000.0f * sinf(2 * 3.14159f * f * i / SAMPLE_RATE));
            if (idx >= 256) {
                size_t w = 0;
                i2s_channel_write(tx_chan, melBuf, 512, &w, 500);
                idx = 0;
            }
        }
        if (idx > 0) {
            size_t w = 0;
            i2s_channel_write(tx_chan, melBuf, idx * 2, &w, 500);
        }
    }
    digitalWrite(LED_PIN, LOW);
    currentState = 0;
    sendFrame(CMD_PLAY_DONE, NULL, 0);
}

// ── WiFi STA ──────────────────────────────────────────────────────
bool loadWiFiCredentials(String &ssid, String &password) {
    nvs.begin("wifi", true);
    ssid = nvs.getString("ssid", "");
    password = nvs.getString("password", "");
    nvs.end();
    return ssid.length() > 0;
}

void saveWiFiCredentials(const String &ssid, const String &password) {
    nvs.begin("wifi", false);
    nvs.putString("ssid", ssid);
    nvs.putString("password", password);
    nvs.end();
}

// ── AP 配网模式 ─────────────────────────────────────────────────
void handleAPRoot() {
    String html = R"rawliteral(
<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>设备配网</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{margin:0;padding:0;box-sizing:border-box;font-family:-apple-system,sans-serif}
body{background:#f5f5f7;display:flex;min-height:100vh;align-items:center;justify-content:center}
.card{background:#fff;border-radius:16px;padding:32px;margin:20px;max-width:400px;width:100%;box-shadow:0 4px 24px rgba(0,0,0,0.08)}
.logo{font-size:36px;margin-bottom:8px}
h1{font-size:20px;font-weight:600;margin-bottom:4px}
.sub{color:#666;font-size:14px;margin-bottom:24px}
input{width:100%;padding:14px;margin:8px 0;border:1px solid #e0e0e0;border-radius:10px;font-size:16px;background:#f9f9f9}
input:focus{outline:none;border-color:#007aff;background:#fff}
button{width:100%;padding:14px;background:#007aff;color:#fff;border:none;border-radius:10px;font-size:16px;font-weight:500;cursor:pointer;margin-top:8px}
button:hover{background:#0056b3}
</style></head><body><div class="card">
<div class="logo">🎙️</div>
<h1>小智 AI 助手</h1>
<p class="sub">请选择家庭 WiFi 网络</p>
<form action="/save" method="POST">
<input name="ssid" placeholder="WiFi 名称 (SSID)" required>
<input name="password" type="password" placeholder="WiFi 密码 (可不填)">
<button type="submit">连接 WiFi</button>
</form></div></body></html>)rawliteral";
    apServer.send(200, "text/html", html);
}

void handleAPSave() {
    String ssid = apServer.arg("ssid");
    String password = apServer.arg("password");
    if (ssid.length() == 0) {
        apServer.send(200, "text/plain", "SSID 不能为空");
        return;
    }
    saveWiFiCredentials(ssid, password);
    apServer.send(200, "text/html",
        "<html><head><meta charset=\"UTF-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<style>"
        "*{margin:0;padding:0;box-sizing:border-box;font-family:-apple-system,sans-serif}"
        "body{background:#f5f5f7;display:flex;min-height:100vh;align-items:center;justify-content:center}"
        ".card{background:#fff;border-radius:16px;padding:32px;margin:20px;max-width:400px;width:100%;text-align:center;box-shadow:0 4px 24px rgba(0,0,0,0.08)}"
        ".icon{font-size:48px;color:#34c759;margin-bottom:16px}"
        "h2{margin-bottom:8px;font-weight:600}"
        "p{color:#666;font-size:14px;margin-bottom:20px}"
        ".hint{background:#f0f0f0;padding:10px;border-radius:8px;font-size:13px;color:#888}"
        "</style>"
        "</head><body><div class=\"card\">"
        "<div class=\"icon\">✓</div>"
        "<h2>配置成功</h2>"
        "<p>WiFi 账号已保存，设备正在重启...</p>"
        "<div class=\"hint\">请确保您的电脑和本设备在同一个家庭 WiFi 网络下</div>"
        "</div></body></html>");
    delay(100);
    Serial.printf("[AP] saved: %s, rebooting...\n", ssid.c_str());
    ESP.restart();
}

void doWifiScan() {
    WiFi.scanNetworks(true);
    while (WiFi.scanComplete() < 0) delay(10);
    int n = WiFi.scanComplete();
    String list;
    for (int i = 0; i < n; i++) {
        if (i > 0) list += "\n";
        list += WiFi.SSID(i);
    }
    WiFi.scanDelete();
    sendFrame(CMD_WIFI_SSIDS, (const uint8_t*)list.c_str(), list.length());
}

void startAPMode() {
    String ap_ssid = AP_SSID_PREFIX + String((uint32_t)(ESP.getEfuseMac() & 0xFFFFFF), HEX);
    WiFi.mode(WIFI_AP_STA);
    WiFi.softAPConfig(IPAddress(192,168,4,1), IPAddress(192,168,4,1), IPAddress(255,255,255,0));
    WiFi.softAP(ap_ssid.c_str());
    delay(200);
    apServer.on("/", handleAPRoot);
    apServer.on("/save", HTTP_POST, handleAPSave);
    apServer.begin();
    Serial.printf("[AP] SSID: %s IP: %s\n", ap_ssid.c_str(), WiFi.softAPIP().toString().c_str());
    apMode = true;
}

// ── WiFi STA ──────────────────────────────────────────────────────
bool startSTA() {
    String ssid, password;
    if (!loadWiFiCredentials(ssid, password)) {
        Serial.println("[WIFI] no credentials");
        return false;
    }
    Serial.printf("[WIFI] connecting to %s ...\n", ssid.c_str());
    WiFi.mode(WIFI_STA);
    WiFi.begin(ssid.c_str(), password.c_str());
    uint32_t t0 = millis();
    while (WiFi.status() != WL_CONNECTED) {
        delay(500);
        Serial.print(".");
        if (millis() - t0 > STA_TIMEOUT_MS) {
            Serial.printf("\n[WIFI] fail (status=%d)\n", WiFi.status());
            WiFi.disconnect();
            return false;
        }
    }
    Serial.printf("\n[WIFI] connected! IP: %s\n", WiFi.localIP().toString().c_str());
    wifiConnected = true;
    useWifi = true;
    tcpServer.begin();
    Serial.printf("[WIFI] TCP on port %d\n", TCP_PORT);
    return true;
}

void wifiTask(void* p) {
    (void)p;
    while (true) {
        if (!wifiConnected) { delay(100); continue; }
        if (!tcpClient || !tcpClient.connected()) {
            tcpClientConnected = false;
            WiFiClient client = tcpServer.available();
            if (client) {
                tcpClient = client;
                tcpClientConnected = true;
                tcpClient.setNoDelay(true);
                Serial.println("[WIFI] TCP client connected");
            } else { delay(10); continue; }
        }
        while (tcpClient.connected() && tcpClient.available()) {
            uint8_t buf[1024];
            int n = tcpClient.read(buf, sizeof(buf));
            if (n > 0) {
                for (int i = 0; i < n; i++) wifiParser.processByte(buf[i]);
            }
        }
        if (!tcpClient.connected()) {
            tcpClientConnected = false;
            Serial.println("[WIFI] TCP client disconnected");
        }
        delay(1);
    }
}

void setup() {
    bootMillis = millis();
    Serial.setRxBufferSize(4096);
    Serial.begin(921600);
    delay(100);

    pinMode(LED_PIN, OUTPUT); digitalWrite(LED_PIN, LOW);
    pinMode(PA_ENABLE, OUTPUT); digitalWrite(PA_ENABLE, HIGH);

    i2c_gpio_init_scl_out();
    i2c_gpio_init_sda_in();
    delay(10);

    Serial.println("[A] boot");

    Serial.println("[D] wifi");
    WiFi.persistent(false);
    String ssid, password;
    if (loadWiFiCredentials(ssid, password)) {
        if (startSTA()) {
            xTaskCreate(wifiTask, "wifi", 4096, NULL, 1, NULL);
        } else {
            startAPMode();
        }
    } else {
        startAPMode();
    }

    i2c_reset_bus();
    Serial.println("[A1] i2c recovered");

    uint8_t test_val = readES8311(0x00);
    Serial.printf("[A2] R00=0x%02X\n", test_val);

    initI2S();
    Serial.println("[C] i2s");

    initES8311();
    Serial.println("[B] es8311");

    Serial.println("[T] beep...");
    int16_t beepBuf[128];
    for (int i = 0; i < 128; i++)
        beepBuf[i] = (int16_t)(20000.0f * sinf(2 * 3.14159f * 440 * i / SAMPLE_RATE));
    for (int n = 0; n < 40; n++) {
        size_t w = 0;
        esp_err_t err = i2s_channel_write(tx_chan, beepBuf, 256, &w, 100);
        if (err != ESP_OK || w != 256) {
            Serial.printf("[T] write err=%d wrote=%d\n", err, w);
        }
    }
    Serial.println("[T] beep done");

    gpio_config_t btn_cfg = {};
    btn_cfg.pin_bit_mask = (1ULL << BOOT_BTN);
    btn_cfg.mode = GPIO_MODE_INPUT;
    btn_cfg.pull_up_en = GPIO_PULLUP_ENABLE;
    btn_cfg.pull_down_en = GPIO_PULLDOWN_DISABLE;
    btn_cfg.intr_type = GPIO_INTR_ANYEDGE;
    gpio_config(&btn_cfg);
    gpio_intr_disable((gpio_num_t)BOOT_BTN);

    uint32_t g9 = gpio_get_level((gpio_num_t)BOOT_BTN);
    Serial.printf("[GPIO9] level=%d\n", g9);

    gpio_install_isr_service(ESP_INTR_FLAG_LEVEL1);

    xTaskCreate(playTask, "play", 4096, NULL, 1, NULL);

    gpio_isr_handler_add((gpio_num_t)BOOT_BTN, btnISR, NULL);
    gpio_intr_enable((gpio_num_t)BOOT_BTN);
    btnPressed = false;
    btnReleased = false;
    while (Serial.available()) Serial.read();
    bootMillis = millis();
    Serial.println("[E] ready");
}

void loop() {
    if (apMode) apServer.handleClient();

    int sb;
    while ((sb = Serial.read()) >= 0) {
        serialParser.processByte((uint8_t)sb);
    }

    static unsigned long lastBtnTime = 0;
    static unsigned long playStart = 0;

    if (playActive && playStart == 0) playStart = millis();
    if (!playActive) playStart = 0;

    {
        static int btnLast = 1;
        static unsigned long btnDebounce = 0;
        int btnNow = gpio_get_level((gpio_num_t)BOOT_BTN);
        if (btnNow != btnLast && millis() - btnDebounce > 80) {
            btnDebounce = millis();
            btnLast = btnNow;
            if (btnNow == 0 && millis() - bootMillis > BOOT_GUARD_MS) {
                if (playActive) {
                    playActive = false; currentState = 0; ringEos = false; ringHead = 0; ringTail = 0;
                } else if (currentState == 0) {
                    startRec();
                } else if (currentState == 2) {
                    playActive = false; currentState = 0; ringEos = false; ringHead = 0; ringTail = 0;
                }
            } else if (currentState == 1) {
                stopRec();
            }
        }
    }

    if (playActive && playStart != 0 && millis() - playStart > PLAY_TIMEOUT_MS) {
        playActive = false;
        currentState = 0;
        ringEos = false;
        ringHead = 0;
        ringTail = 0;
        playStart = 0;
        sendFrame(CMD_PLAY_DONE, NULL, 0);
    }

    if (currentState == 1) {
        if (millis() - stateStart > REC_TIMEOUT_MS) stopRec();
        else processRec();
    }

    delay(1);
}
