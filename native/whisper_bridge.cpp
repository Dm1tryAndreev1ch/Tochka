// Narrow C ABI around the installed whisper.cpp; no model ABI structs cross into Python.
#include <whisper.h>
#include <ggml-backend.h>
#include <algorithm>
#include <cmath>
#include <cstring>
#include <string>

static void quiet_log(enum ggml_log_level, const char *, void *) {}

extern "C" {
void *voice_whisper_create(const char *path, int use_gpu) {
    ggml_log_set(quiet_log, nullptr);
    ggml_backend_load_all_from_path(GGML_BACKEND_DIR);
    if (ggml_backend_dev_count() == 0) return nullptr;
    whisper_log_set(quiet_log, nullptr);
    auto params = whisper_context_default_params();
    params.use_gpu = use_gpu != 0;
    params.flash_attn = true;
    return whisper_init_from_file_with_params(path, params);
}
void voice_whisper_free(void *context) {
    if (context) whisper_free(static_cast<whisper_context *>(context));
}
const char *voice_whisper_version() { return whisper_version(); }

int voice_whisper_transcribe(void *context, const float *samples, int count,
                             char *output, int capacity, int audio_context) {
    if (!context || !samples || count < 160 || count > 16000 * 21 || capacity < 2) return -1;
    auto ctx = static_cast<whisper_context *>(context);
    auto params = whisper_full_default_params(WHISPER_SAMPLING_BEAM_SEARCH);
    params.language = "ru";
    params.n_threads = 4;
    params.no_context = true;
    params.single_segment = true;
    params.no_timestamps = true;
    params.print_special = false;
    params.print_progress = false;
    params.print_realtime = false;
    params.print_timestamps = false;
    params.temperature = 0;
    params.temperature_inc = 0;
    params.beam_search.beam_size = 3;
    params.beam_search.patience = 1.0f;
    params.suppress_nst = true;
    params.max_tokens = 160;
    // Optional, benchmarked shorter context for utterances already segmented by Silero.
    params.audio_ctx = audio_context;
    params.initial_prompt = "Здравствуйте. Консультация, диагностика, настройка. Беларусь, Минск. Телефон плюс триста семьдесят пять.";
    int result = whisper_full(ctx, params, samples, count);
    if (result != 0) return result;
    std::string text;
    for (int i = 0; i < whisper_full_n_segments(ctx); ++i) {
        if (whisper_full_get_segment_no_speech_prob(ctx, i) < 0.8f)
            text += whisper_full_get_segment_text(ctx, i);
    }
    if (static_cast<int>(text.size()) >= capacity) return -2;
    std::memcpy(output, text.data(), text.size());
    output[text.size()] = '\0';
    return 0;
}
}
