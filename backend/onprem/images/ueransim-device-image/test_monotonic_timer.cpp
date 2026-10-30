// Contract test against the patched real UERANSIM TimerBase implementation.
#include <cassert>
#include <cstdint>
#include <utils/nts.hpp>

static int64_t wall = 1793134672000;
static int64_t monotonic = 10000;
namespace utils {
int64_t CurrentTimeMillis() { return wall; }
int64_t CurrentMonotonicMillis() { return monotonic; }
}
int main() {
    TimerBase timer;
    timer.setTimerAbsolute(7, monotonic + 2000);
    wall += 2932; // Actual observed WSL forward clock correction.
    assert(timer.getAndRemoveExpiredTimer() == nullptr);
    assert(timer.getNextWaitTime() == 2000);
    wall -= 60000; // Backward correction must not delay the relative timer.
    monotonic += 1999;
    assert(timer.getAndRemoveExpiredTimer() == nullptr);
    monotonic += 2;
    auto *expired = timer.getAndRemoveExpiredTimer();
    assert(expired != nullptr && expired->timerId == 7);
    delete expired;
    assert(timer.getAndRemoveExpiredTimer() == nullptr);
}
