// Small shared helpers: RAII lock for the single Board+ControlHub mutex.
#pragma once
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

namespace bbfw {

// Blocking lock. Used by httpd-thread handlers and the esp_timer work item only.
// The render task must NOT use this (it uses a bounded xSemaphoreTake instead).
class MutexLock {
 public:
  explicit MutexLock(SemaphoreHandle_t m) : m_(m) { xSemaphoreTake(m_, portMAX_DELAY); }
  ~MutexLock() { xSemaphoreGive(m_); }
  MutexLock(const MutexLock&) = delete;
  MutexLock& operator=(const MutexLock&) = delete;

 private:
  SemaphoreHandle_t m_;
};

}  // namespace bbfw
