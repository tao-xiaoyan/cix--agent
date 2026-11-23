#include <libobsensor/ObSensor.hpp>
#include <iostream>
#include <fstream>
#include <memory>
#include <chrono>
#include <cstdint>
#include <csignal>
#include <cstdio>
#include <algorithm>
#include <cmath>
#include <condition_variable>
#include <iomanip>
#include <limits>
#include <locale>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <utility>
#include <vector>
#include <time.h>

volatile std::sig_atomic_t running = 1;
void stopHandler(int) { running = 0; }

void write16(std::ofstream &f, uint16_t v) {
    f.put(char(v)); f.put(char(v >> 8));
}
void write32(std::ofstream &f, uint32_t v) {
    for(int i = 0; i < 4; ++i)
        f.put(char(v >> (i * 8)));
}

bool saveBMP(const uint16_t *data, int w, int h, float scale) {
    const char *tmp = "live/depth.tmp";
    const char *dst = "live/depth.bmp";

    int stride = (w * 3 + 3) & ~3;
    uint32_t imageSize = stride * h;

    std::ofstream f(tmp, std::ios::binary);
    if(!f) return false;

    f.put('B'); f.put('M');
    write32(f, 54 + imageSize);
    write16(f, 0); write16(f, 0);
    write32(f, 54);
    write32(f, 40);
    write32(f, w); write32(f, h);
    write16(f, 1); write16(f, 24);
    write32(f, 0); write32(f, imageSize);
    for(int i = 0; i < 4; ++i) write32(f, 0);

    for(int y = h - 1; y >= 0; --y) {
        for(int x = 0; x < w; ++x) {
            uint16_t raw = data[y * w + x];
            unsigned char value = 0;

            if(raw != 0) {
                float mm = raw * scale;
                float t = std::clamp(
                    (mm - 300.0f) / 3200.0f, 0.0f, 1.0f
                );
                value = static_cast<unsigned char>(
                    255.0f * (1.0f - t)
                );
            }

            for(int c = 0; c < 3; ++c)
                f.put(char(value));
        }
        for(int p = w * 3; p < stride; ++p)
            f.put(0);
    }

    f.close();
    if(!f) return false;

    return std::rename(tmp, dst) == 0;
}


struct Y16Snapshot {
    std::vector<uint16_t> pixels;
    uint64_t index, deviceUs, receivedUnixUs, receivedMonotonicNs;
    uint32_t width, height;
    float scale;
};

class Y16Writer {
public:
    Y16Writer() {
        std::ifstream uuid("/proc/sys/kernel/random/uuid");
        std::ifstream boot("/proc/sys/kernel/random/boot_id");
        if(uuid >> sessionId && boot >> bootId) {
            try {
                worker = std::thread(&Y16Writer::run, this);
            } catch(const std::system_error &error) {
                std::cerr << "Y16 snapshot disabled: " << error.what() << '\n';
            }
        } else {
            std::cerr << "Y16 snapshot disabled: missing session or boot ID\n";
        }
    }

    ~Y16Writer() {
        {
            std::lock_guard<std::mutex> lock(mutex);
            stopping = true;
        }
        ready.notify_one();
        if(worker.joinable()) worker.join();
    }

    void offer(const ob::DepthFrame &depth, const uint16_t *data, uint32_t width,
               uint32_t height, float scale, uint64_t unixUs, uint64_t monotonicNs) {
        if(!worker.joinable()) return;
        const uint64_t count = uint64_t(width) * height;
        if(!count || count != depth.getDataSize() / sizeof(uint16_t)
           || depth.getDataSize() % sizeof(uint16_t) || !std::isfinite(scale) || scale <= 0) {
            std::cerr << "Y16 snapshot skipped: invalid frame layout or scale\n";
            return;
        }
        const auto index = depth.getIndex();
        const auto deviceUs = depth.getTimeStampUs();
        std::unique_lock<std::mutex> lock(mutex, std::try_to_lock);
        if(!lock) return;

        pending.pixels.assign(data, data + count);
        pending.index = index;
        pending.deviceUs = deviceUs;
        pending.receivedUnixUs = unixUs;
        pending.receivedMonotonicNs = monotonicNs;
        pending.width = width;
        pending.height = height;
        pending.scale = scale;
        hasPending = true;
        lock.unlock();
        ready.notify_one();
    }

private:
    bool publish(const Y16Snapshot &frame) {
        static_assert(__BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__, "Y16 writer requires little endian");
        std::ostringstream json;
        json.imbue(std::locale::classic());
        json << std::setprecision(std::numeric_limits<float>::max_digits10)
             << "{\"session_id\":\"" << sessionId << "\",\"boot_id\":\"" << bootId
             << "\",\"frame_index\":" << frame.index
             << ",\"device_timestamp_us\":" << frame.deviceUs
             << ",\"device_clock\":\"camera\",\"received_unix_us\":" << frame.receivedUnixUs
             << ",\"received_monotonic_ns\":" << frame.receivedMonotonicNs
             << ",\"width\":" << frame.width << ",\"height\":" << frame.height
             << ",\"format\":\"Y16\",\"storage\":\"uint16_le\",\"value_scale_mm\":" << frame.scale
             << ",\"invalid_value\":0}";
        const auto metadata = json.str();

        const char *tmp = "live/latest.y16frame.tmp";
        std::ofstream file(tmp, std::ios::binary | std::ios::trunc);
        if(!file) return false;
        file.write("Y16F", 4);
        write32(file, 1);
        write32(file, metadata.size());
        file.write(metadata.data(), metadata.size());
        file.write(reinterpret_cast<const char *>(frame.pixels.data()),
                   frame.pixels.size() * sizeof(uint16_t));
        file.close();
        if(file && std::rename(tmp, "live/latest.y16frame") == 0) return true;
        std::remove(tmp);
        return false;
    }

    void run() {
        Y16Snapshot frame{};
        bool reportedFailure = false;
        for(;;) {
            {
                std::unique_lock<std::mutex> lock(mutex);
                ready.wait(lock, [this] { return stopping || hasPending; });
                if(stopping) return;
                std::swap(frame, pending);
                hasPending = false;
            }
            const bool written = publish(frame);
            if(!written && !reportedFailure) std::cerr << "Y16 snapshot write failed\n";
            reportedFailure = !written;
        }
    }

    std::string sessionId, bootId;
    Y16Snapshot pending{};
    std::mutex mutex;
    std::condition_variable ready;
    bool hasPending = false, stopping = false;
    std::thread worker;
};

int main() {
    std::signal(SIGINT, stopHandler);
    std::signal(SIGTERM, stopHandler);
    using Clock = std::chrono::steady_clock;

    try {
        ob::Pipeline pipe;
        auto config = std::make_shared<ob::Config>();
        config->enableVideoStream(OB_STREAM_DEPTH);
        pipe.start(config);
        Y16Writer y16;

        std::cout << "LIVE DEPTH STARTED" << std::endl;

        uint64_t frames = 0, previews = 0;
        auto start = Clock::now();
        auto lastPreview = start - std::chrono::seconds(1);
        auto lastReport = start;

        while(running) {
            auto fs = pipe.waitForFrameset(1000);
            if(!fs) continue;

            auto raw = fs->getFrame(OB_FRAME_DEPTH);
            if(!raw) continue;

            auto depth = raw->as<ob::DepthFrame>();
            if(depth->getFormat() != OB_FORMAT_Y16) continue;

            ++frames;
            auto now = Clock::now();

            if(std::chrono::duration<double>(
                now - lastPreview).count() >= 0.2) {

                auto data = reinterpret_cast<const uint16_t*>(
                    depth->getData()
                );

                if(data) {
                    const auto width = depth->getWidth();
                    const auto height = depth->getHeight();
                    const auto scale = depth->getValueScale();
                    const auto unixUs = std::chrono::duration_cast<std::chrono::microseconds>(
                        std::chrono::system_clock::now().time_since_epoch()).count();
                    timespec received{};
                    const bool hasMonotonic = clock_gettime(CLOCK_MONOTONIC, &received) == 0;
                    if(saveBMP(data, width, height, scale)) ++previews;
                    if(hasMonotonic) {
                        const uint64_t monotonicNs = uint64_t(received.tv_sec) * 1000000000
                            + received.tv_nsec;
                        try {
                            y16.offer(*depth, data, width, height, scale, unixUs, monotonicNs);
                        } catch(const std::exception &error) {
                            std::cerr << "Y16 snapshot skipped: " << error.what() << '\n';
                        }
                    }
                }
                lastPreview = now;
            }

            if(std::chrono::duration<double>(
                now - lastReport).count() >= 10.0) {
                double elapsed =
                    std::chrono::duration<double>(
                        now - start).count();

                std::cout << "Capture FPS: "
                          << frames / elapsed
                          << " | Previews: " << previews
                          << std::endl;
                lastReport = now;
            }
        }

        pipe.stop();
        std::cout << "LIVE DEPTH STOPPED" << std::endl;
        return 0;

    } catch(const ob::Error &e) {
        std::cerr << e.what() << std::endl;
        return 1;
    }
}
