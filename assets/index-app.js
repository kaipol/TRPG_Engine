const { createApp, ref, onMounted, watch, computed, nextTick } = Vue;
const API_BASE_URL = window.location.protocol.startsWith('http') ? window.location.origin : "http://localhost:8000";
const CAMPAIGN_IMPORT_POLL_TIMEOUT_MS = 35 * 60 * 1000;
const CAMPAIGN_IMPORT_NO_PROGRESS_TIMEOUT_MS = 10 * 60 * 1000;
const MINERU_FLASH_MAX_BYTES = 10 * 1024 * 1024;
const CONFIG_MODEL_OPTIONS_CACHE_KEY = 'zric-config-model-options-v1';
const SOLO_SESSION_LOG_LIMIT = 80;
const HIDDEN_ROOM_SAVE_PREFIX = '__room_';
const SOLO_ROOM_SAVE_PREFIX = '__room_solo_';
const MULTIPLAYER_ROOM_SAVE_PREFIX = '__room_mp_';
const SOLO_SESSIONS_KEY = 'zric-solo-sessions';
const SOLO_SESSIONS_LEGACY_KEYS = ['zric-solo-sessions-v1'];

createApp({
    setup() {
        const appState = ref('menu');
        const playSurface = ref('player');
        const pendingLaunchMode = ref('');
        const readStoredJson = (key, fallback) => {
            try {
                return JSON.parse(localStorage.getItem(key) || '') || fallback;
            } catch(e) {
                return fallback;
            }
        };
        const readOrCreateMultiplayerClientId = () => {
            let id = localStorage.getItem('zric-mp-client-id') || '';
            if (!id) {
                id = `client-${crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2, 14)}`;
                localStorage.setItem('zric-mp-client-id', id);
            }
            return id;
        };
        const isLoading = ref(false);
        const isDeletingCampaign = ref(false);
        const campaignPackageImportBusy = ref(false);
        const campaignPackageExportBusy = ref(false);
        const dbConnected = ref(false);
        const campaignFiles = ref([]);
        const saveFiles = ref([]);
        const campaignLoadSummary = ref('');
        const showGameSettingsModal = ref(false);
        const showAdvancedSettings = ref(false);
        const openGameSettingsModal = () => {
            showAdvancedSettings.value = false;
            showGameSettingsModal.value = true;
        };
        const showCampaignImportModal = ref(false);
        const campaignImportName = ref('');
        const campaignImportMainFile = ref(null);
        const campaignImportAssets = ref([]);
        const campaignImportOcrEnabled = ref(true);
        const campaignImportBusy = ref(false);
        const campaignImportResult = ref(null);
        const campaignImportWarnings = ref([]);
        const campaignImportErrorText = ref('');
        const campaignImportJobId = ref('');
        const campaignImportProgress = ref(0);
        const campaignImportProgressStep = ref('等待');
        const campaignImportProgressMessage = ref('等待上传剧本文档');
        const campaignImportVerifiedNotice = ref(null);
        const campaignImportOutcome = ref(null);
        let campaignImportNoticeTimer = null;
        const campaignImportFormats = ref([]);
        const campaignImportFormatsText = computed(() => {
            const exts = (campaignImportFormats.value || []).map(f => f.ext).filter(Boolean);
            return (exts.length ? exts.join(' / ') : '.pdf / .docx / .doc / .txt / .md') + '，PDF/Word 使用 MinerU 解析';
        });
        const formatCampaignImportSize = (bytes) => {
            const n = Number(bytes || 0);
            if (n <= 0) return '0 B';
            if (n < 1024) return `${n} B`;
            if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
            return `${(n / 1024 / 1024).toFixed(1)} MB`;
        };
        const campaignImportFileExt = computed(() => {
            const name = campaignImportMainFile.value?.name || '';
            const match = name.toLowerCase().match(/\.[^.]+$/);
            return match ? match[0] : '';
        });
        const campaignImportUsesMinerU = computed(() => ['.pdf', '.docx', '.doc'].includes(campaignImportFileExt.value));
        const campaignImportMinerUModeText = computed(() => {
            const file = campaignImportMainFile.value;
            if (!file) return '选择文件后自动判断 MinerU 模式';
            if (!campaignImportUsesMinerU.value) return 'TXT/Markdown 按纯文本导入，不调用 MinerU';
            const sizeText = formatCampaignImportSize(file.size);
            if (campaignImportFileExt.value === '.doc') return `${sizeText}，DOC 仅支持 extract，需要 MinerU token`;
            if ((file.size || 0) > MINERU_FLASH_MAX_BYTES) {
                return `${sizeText}，超过 flash-extract 10MB 限制，将使用 extract（需要 token）`;
            }
            return `${sizeText}，优先使用 flash-extract（免 token），失败时尝试 extract`;
        });
        const campaignImportVisibleWarnings = computed(() => {
            return (campaignImportWarnings.value || []).filter(Boolean).slice(-6);
        });
        const formatCampaignImportError = (value, fallback = '剧本解析失败') => {
            if (!value) return fallback;
            if (typeof value === 'string') return value;
            if (Array.isArray(value)) {
                return value.map(item => formatCampaignImportError(item, '')).filter(Boolean).join('；') || fallback;
            }
            if (typeof value === 'object') {
                if (value.msg || value.message || value.detail) {
                    return [value.loc ? `${Array.isArray(value.loc) ? value.loc.join('.') : value.loc}` : '', value.msg || value.message || value.detail]
                        .filter(Boolean)
                        .join('：');
                }
                try {
                    return JSON.stringify(value);
                } catch(e) {
                    return fallback;
                }
            }
            return String(value);
        };

        // ── API 供应商配置 ──
        const showApiKeyPanel = ref(false);
        const openApiKeyPanel = async () => {
            showGameSettingsModal.value = false;
            showApiKeyPanel.value = true;
            apiKeySaveMsg.value = '';
            await fetchApiKeyStatus({ resetModels: true });
            loadModelOptionsCache();
            syncConfiguredModelState();
        };
        const apiKeyStatus = ref({ openai: false });
        const apiProviders = ref([]);
        const activeProviderId = ref('');
        const activeAiProvider = ref(null);
        const tokenPolicy = ref({
            mode: 'full',
            active: null,
            modes: [
                { mode: 'full', label: '完整', rag_top_k: 0, output_token_cap: 0, json_output_token_cap: 0 },
                { mode: 'balanced', label: '平衡', rag_top_k: 4, output_token_cap: 1000, json_output_token_cap: 1200 },
                { mode: 'frugal', label: '极省', rag_top_k: 2, output_token_cap: 700, json_output_token_cap: 950 },
            ],
        });
        const isSavingTokenPolicy = ref(false);
        const activeProviderName = computed(() => {
            const active = activeAiProvider.value;
            if (active?.name) return active.name;
            return apiProviders.value.find(p => p.id === activeProviderId.value)?.name || '';
        });
        const blankConfigModelSearches = () => ({ chat: '', embedding: '', image: '', campaign: '' });
        const dropdownModelSearches = ref(blankConfigModelSearches());
        const openModelDropdownCapability = ref('');
        const blankModelOptions = () => ({ chat: [], embedding: [], image: [], campaign: [] });
        const modelOptions = ref(blankModelOptions());
        const isFetchingConfigModels = ref(false);
        const fetchingConfigCapability = ref('');
        const modelConfigFields = [
            { key: 'chatModel', capability: 'chat', label: 'Chat Model', placeholder: 'gpt-4.1 / qwen-plus / custom-chat-model' },
            { key: 'embeddingModel', capability: 'embedding', label: 'Embedding Model', placeholder: 'text-embedding-3-small / bge-m3' },
            { key: 'imageModel', capability: 'image', label: 'Image Model', placeholder: 'gpt-image-1 / flux-kontext' },
            { key: 'campaignModel', capability: 'campaign', label: '剧本解析模型', placeholder: 'gpt-4.1 / qwen-long / vision-capable-model' },
        ];
        const blankApiKeyInputs = (providerName = '') => ({
            providerId: '',
            providerName,
            openaiApiKey: '',
            baseUrl: 'https://api.openai.com/v1',
            chatModel: '',
            embeddingModel: '',
            imageModel: '',
            campaignModel: '',
            imageSize: '1024x1024',
        });
        const apiKeyInputs = ref({
            ...blankApiKeyInputs(),
        });
        const apiKeysMissing = computed(() => !apiKeyStatus.value.openai);
        const isSavingKeys = ref(false);
        const apiKeySaveMsg = ref('');
        const apiKeySaveOk = ref(true);
        const aiCache = ref({
            enabled: true,
            max_entries: 512,
            entries: 0,
            total_hits: 0,
            saved_prompt_chars: 0,
            saved_response_chars: 0,
        });
        const isSavingAiCache = ref(false);

        const configRequestHeaders = () => {
            const headers = { 'Content-Type': 'application/json' };
            if (multiplayerAuthToken.value) headers['X-Auth-Token'] = multiplayerAuthToken.value;
            return headers;
        };
        const requireProviderConfigUi = () => {
            if (multiplayerAuthAccount.value) return true;
            apiKeySaveOk.value = false;
            apiKeySaveMsg.value = '请先登录账号';
            return false;
        };

        const applyTokenPolicyPayload = (payload) => {
            const data = payload?.token_policy || payload;
            if (!data || !data.mode) return;
            tokenPolicy.value = {
                mode: data.mode || tokenPolicy.value.mode || 'full',
                active: data.active || tokenPolicy.value.active || null,
                modes: Array.isArray(data.modes) && data.modes.length ? data.modes : tokenPolicy.value.modes,
            };
        };

        const activeTokenPolicyMode = () => {
            return (tokenPolicy.value.modes || []).find(m => m.mode === tokenPolicy.value.mode)
                || tokenPolicy.value.active
                || (tokenPolicy.value.modes || [])[0]
                || {};
        };

        const tokenPolicySummary = (mode) => {
            if (!mode) return '';
            const ragTopK = Number(mode.rag_top_k || 0);
            const outputCap = Number(mode.output_token_cap || 0);
            const jsonCap = Number(mode.json_output_token_cap || 0);
            if (!ragTopK && !outputCap && !jsonCap) return '完整上下文';
            const parts = [];
            if (ragTopK) parts.push(`RAG ${ragTopK}`);
            if (outputCap) parts.push(`输出 ${outputCap}`);
            if (jsonCap && jsonCap !== outputCap) parts.push(`JSON ${jsonCap}`);
            return parts.join(' / ');
        };

        const applyAiCachePayload = (payload) => {
            const data = payload?.ai_cache || payload;
            if (!data) return;
            aiCache.value = {
                enabled: data.enabled !== false,
                max_entries: Number(data.max_entries || aiCache.value.max_entries || 512),
                entries: Number(data.entries || 0),
                total_hits: Number(data.total_hits || 0),
                saved_prompt_chars: Number(data.saved_prompt_chars || 0),
                saved_response_chars: Number(data.saved_response_chars || 0),
                oldest_at: data.oldest_at || '',
                newest_at: data.newest_at || '',
            };
        };

        const aiCacheSummary = computed(() => {
            const entries = Number(aiCache.value.entries || 0);
            const hits = Number(aiCache.value.total_hits || 0);
            const savedChars = Number(aiCache.value.saved_prompt_chars || 0) + Number(aiCache.value.saved_response_chars || 0);
            const savedText = savedChars >= 10000 ? `${Math.round(savedChars / 1000)}k 字符` : `${savedChars} 字符`;
            return `${entries} 条 / 命中 ${hits} / 约省 ${savedText}`;
        });

        const providerToConfigShape = (provider = {}) => ({
            provider_id: provider.id || '',
            provider_name: provider.name || '',
            base_url: provider.base_url || '',
            chat_model: provider.chat_model || '',
            embedding_model: provider.embedding_model || '',
            image_model: provider.image_model || '',
            campaign_model: provider.campaign_model || '',
            image_size: provider.image_size || '1024x1024',
        });

        const applyApiConfigPayload = (d, { keepTypedKey = false } = {}) => {
            if (!d || d.status !== 'success') return;
            applyTokenPolicyPayload(d);
            applyAiCachePayload(d);
            const providers = Array.isArray(d.providers) ? d.providers : (apiProviders.value || []);
            apiProviders.value = providers;
            const requestedActiveId = d.active_provider || d.config?.provider_id || activeProviderId.value;
            let activeProfile = providers.find(p => p.id === requestedActiveId);
            if (providers.length && !activeProfile) {
                activeProfile = providers.find(p => p.active) || providers.find(p => p.configured) || providers[0];
            }
            activeProviderId.value = activeProfile?.id || requestedActiveId || '';
            const configured = d.keys?.openai_compatible?.configured;
            const providerConfig = activeProfile ? providerToConfigShape(activeProfile) : null;
            const configMatchesActive = !providerConfig || !d.config?.provider_id || d.config.provider_id === providerConfig.provider_id;
            const formConfig = configMatchesActive
                ? { ...(providerConfig || {}), ...(d.config || {}) }
                : providerConfig;
            activeAiProvider.value = formConfig ? {
                id: formConfig.provider_id || activeProviderId.value || '',
                name: formConfig.provider_name || activeProfile?.name || '',
                base_url: formConfig.base_url || activeProfile?.base_url || '',
            } : activeAiProvider.value;
            apiKeyStatus.value = { openai: activeProfile ? !!activeProfile.configured : !!configured };
            if (formConfig) {
                apiKeyInputs.value = {
                    ...apiKeyInputs.value,
                    providerId: formConfig.provider_id || activeProviderId.value || '',
                    providerName: formConfig.provider_name || activeProfile?.name || '',
                    openaiApiKey: keepTypedKey ? apiKeyInputs.value.openaiApiKey : '',
                    baseUrl: formConfig.base_url || 'https://api.openai.com/v1',
                    chatModel: formConfig.chat_model || '',
                    embeddingModel: formConfig.embedding_model || '',
                    imageModel: formConfig.image_model || '',
                    campaignModel: formConfig.campaign_model || '',
                    imageSize: formConfig.image_size || '1024x1024',
                };
                imgModel.value = formConfig.image_model || imgModel.value;
            }
        };

        const fillProviderInputs = (provider) => {
            if (!provider) return;
            activeProviderId.value = provider.id || '';
            apiKeyStatus.value = { openai: !!provider.configured };
            apiKeyInputs.value = {
                ...apiKeyInputs.value,
                providerId: provider.id || '',
                providerName: provider.name || '',
                openaiApiKey: '',
                baseUrl: provider.base_url || 'https://api.openai.com/v1',
                chatModel: provider.chat_model || '',
                embeddingModel: provider.embedding_model || '',
                imageModel: provider.image_model || '',
                campaignModel: provider.campaign_model || '',
                imageSize: provider.image_size || '1024x1024',
            };
            imgModel.value = provider.image_model || imgModel.value;
        };

        const newApiProvider = () => {
            if (!requireProviderConfigUi()) return;
            activeProviderId.value = '';
            apiKeyStatus.value = { openai: false };
            apiKeyInputs.value = blankApiKeyInputs(`Provider ${apiProviders.value.length + 1}`);
            modelOptions.value = blankModelOptions();
            dropdownModelSearches.value = blankConfigModelSearches();
            openModelDropdownCapability.value = '';
            apiKeySaveMsg.value = '';
        };

        const fetchApiKeyStatus = async ({ resetModels = false } = {}) => {
            if (resetModels) {
                modelOptions.value = blankModelOptions();
                dropdownModelSearches.value = blankConfigModelSearches();
                openModelDropdownCapability.value = '';
            }
            try {
                const r = await fetch(`${API_BASE_URL}/api/config/keys`, { headers: configRequestHeaders() });
                const d = await r.json();
                if (d.status === 'success') {
                    applyApiConfigPayload(d);
                    loadModelOptionsCache();
                    syncConfiguredModelState();
                }
            } catch(e) {}
        };

        const setTokenPolicyMode = async (mode) => {
            const targetMode = (mode || '').trim();
            if (!targetMode || isSavingTokenPolicy.value || tokenPolicy.value.mode === targetMode) return;
            if (!requireProviderConfigUi()) return;
            isSavingTokenPolicy.value = true;
            apiKeySaveMsg.value = '';
            try {
                const r = await fetch(`${API_BASE_URL}/api/config/token-policy`, {
                    method: 'POST',
                    headers: configRequestHeaders(),
                    body: JSON.stringify({ mode: targetMode }),
                });
                const d = await r.json();
                if (d.status === 'success') {
                    applyTokenPolicyPayload(d);
                    apiKeySaveOk.value = true;
                    apiKeySaveMsg.value = 'Token 策略已更新';
                    setTimeout(() => { apiKeySaveMsg.value = ''; }, 2200);
                } else {
                    apiKeySaveOk.value = false;
                    apiKeySaveMsg.value = d.message || 'Token 策略更新失败';
                }
            } catch(e) {
                apiKeySaveOk.value = false;
                apiKeySaveMsg.value = 'Token 策略更新失败';
            } finally {
                isSavingTokenPolicy.value = false;
            }
        };

        const fetchAiCacheStatus = async () => {
            try {
                const r = await fetch(`${API_BASE_URL}/api/config/ai-cache`);
                const d = await r.json();
                if (d.status === 'success') applyAiCachePayload(d);
            } catch(e) {}
        };

        const setAiCacheEnabled = async (enabled) => {
            if (isSavingAiCache.value) return;
            if (!requireProviderConfigUi()) return;
            isSavingAiCache.value = true;
            apiKeySaveMsg.value = '';
            try {
                const r = await fetch(`${API_BASE_URL}/api/config/ai-cache`, {
                    method: 'POST',
                    headers: configRequestHeaders(),
                    body: JSON.stringify({ enabled }),
                });
                const d = await r.json();
                if (d.status === 'success') {
                    applyAiCachePayload(d);
                    apiKeySaveOk.value = true;
                    apiKeySaveMsg.value = enabled ? 'AI 缓存已启用' : 'AI 缓存已关闭';
                    setTimeout(() => { apiKeySaveMsg.value = ''; }, 2200);
                } else {
                    apiKeySaveOk.value = false;
                    apiKeySaveMsg.value = d.message || 'AI 缓存更新失败';
                }
            } catch(e) {
                apiKeySaveOk.value = false;
                apiKeySaveMsg.value = 'AI 缓存更新失败';
            } finally {
                isSavingAiCache.value = false;
            }
        };

        const clearAiCache = async () => {
            if (isSavingAiCache.value || !aiCache.value.entries) return;
            if (!requireProviderConfigUi()) return;
            isSavingAiCache.value = true;
            apiKeySaveMsg.value = '';
            try {
                const r = await fetch(`${API_BASE_URL}/api/config/ai-cache/clear`, {
                    method: 'POST',
                    headers: configRequestHeaders(),
                });
                const d = await r.json();
                if (d.status === 'success') {
                    applyAiCachePayload(d);
                    apiKeySaveOk.value = true;
                    apiKeySaveMsg.value = `已清空 ${d.deleted || 0} 条缓存`;
                    setTimeout(() => { apiKeySaveMsg.value = ''; }, 2200);
                } else {
                    apiKeySaveOk.value = false;
                    apiKeySaveMsg.value = d.message || '清空缓存失败';
                }
            } catch(e) {
                apiKeySaveOk.value = false;
                apiKeySaveMsg.value = '清空缓存失败';
            } finally {
                isSavingAiCache.value = false;
            }
        };

        const switchApiProvider = async (providerId) => {
            if (!providerId) return;
            if (!requireProviderConfigUi()) return;
            const localProfile = apiProviders.value.find(p => p.id === providerId);
            if (localProfile) fillProviderInputs(localProfile);
            modelOptions.value = blankModelOptions();
            dropdownModelSearches.value = blankConfigModelSearches();
            openModelDropdownCapability.value = '';
            loadModelOptionsCache();
            syncConfiguredModelState();
            try {
                const r = await fetch(`${API_BASE_URL}/api/config/providers/switch`, {
                    method: 'POST',
                    headers: configRequestHeaders(),
                    body: JSON.stringify({ provider_id: providerId })
                });
                const d = await r.json();
                if (d.status === 'success') {
                    applyApiConfigPayload(d);
                    loadModelOptionsCache();
                    syncConfiguredModelState();
                } else {
                    apiKeySaveOk.value = false;
                    apiKeySaveMsg.value = d.message || '切换失败';
                }
            } catch(e) {
                apiKeySaveOk.value = false;
                apiKeySaveMsg.value = '切换供应商失败';
            }
        };

        const deleteApiProvider = async (providerId) => {
            if (!providerId || !confirm('删除这个 OpenAI 兼容供应商配置？')) return;
            if (!requireProviderConfigUi()) return;
            try {
                const r = await fetch(`${API_BASE_URL}/api/config/providers/delete`, {
                    method: 'POST',
                    headers: configRequestHeaders(),
                    body: JSON.stringify({ provider_id: providerId })
                });
                const d = await r.json();
                if (d.status === 'success') {
                    apiKeySaveOk.value = true;
                    apiKeySaveMsg.value = '已删除';
                    removeModelOptionsCacheByProviderId(providerId);
                    applyApiConfigPayload(d);
                    loadModelOptionsCache();
                    syncConfiguredModelState();
                    setTimeout(() => { apiKeySaveMsg.value = ''; }, 2500);
                } else {
                    apiKeySaveOk.value = false;
                    apiKeySaveMsg.value = d.message || '删除失败';
                }
            } catch(e) {
                apiKeySaveOk.value = false;
                apiKeySaveMsg.value = '删除供应商失败';
            }
        };

        const ensureModelOption = (models, modelId, provider) => {
            const modelKey = (modelId || '').trim();
            const list = Array.isArray(models) ? [...models] : [];
            if (modelKey && !list.some(m => m.key === modelKey)) {
                list.unshift({
                    key: modelKey,
                    model_id: modelKey,
                    label: modelKey,
                    available: apiKeyStatus.value.openai,
                    provider: provider?.name || activeProviderName.value,
                    provider_id: provider?.id || activeProviderId.value,
                });
            }
            return list;
        };

        const mergeModelOptions = (...modelLists) => {
            const seen = new Set();
            const merged = [];
            modelLists.flat().forEach(model => {
                const key = (model?.key || model?.model_id || '').trim();
                if (!key || seen.has(key)) return;
                seen.add(key);
                merged.push({ ...model, key, model_id: model.model_id || key, label: model.label || key });
            });
            return merged;
        };

        const modelCacheProviderKey = (provider = null) => {
            const providerInfo = provider || activeAiProvider.value || {};
            const providerId = (apiKeyInputs.value.providerId || providerInfo.id || activeProviderId.value || 'draft').trim();
            const baseUrl = (apiKeyInputs.value.baseUrl || providerInfo.base_url || '').trim().toLowerCase();
            return `${providerId || 'draft'}|${baseUrl || 'no-base-url'}`;
        };

        const compactCachedModel = (model) => {
            const key = (model?.key || model?.model_id || '').trim();
            if (!key) return null;
            return {
                key,
                model_id: model.model_id || key,
                label: model.label || key,
                available: model.available !== false,
                provider: model.provider || activeProviderName.value || '',
                provider_id: model.provider_id || activeProviderId.value || '',
            };
        };

        const readModelOptionsCache = () => {
            const data = readStoredJson(CONFIG_MODEL_OPTIONS_CACHE_KEY, {});
            return data && typeof data === 'object' ? data : {};
        };

        const writeModelOptionsCache = (cache) => {
            try {
                localStorage.setItem(CONFIG_MODEL_OPTIONS_CACHE_KEY, JSON.stringify(cache));
            } catch(e) {}
        };

        const saveModelOptionsCache = (provider = null) => {
            const options = {};
            modelConfigFields.forEach(field => {
                options[field.capability] = (modelOptions.value[field.capability] || [])
                    .map(compactCachedModel)
                    .filter(Boolean);
            });
            if (!Object.values(options).some(list => Array.isArray(list) && list.length)) return;
            const cache = readModelOptionsCache();
            cache[modelCacheProviderKey(provider)] = {
                updated_at: new Date().toISOString(),
                provider: {
                    id: (apiKeyInputs.value.providerId || provider?.id || activeProviderId.value || '').trim(),
                    name: (apiKeyInputs.value.providerName || provider?.name || activeProviderName.value || '').trim(),
                    base_url: (apiKeyInputs.value.baseUrl || provider?.base_url || '').trim(),
                },
                options,
            };
            const entries = Object.entries(cache)
                .sort((a, b) => String(b[1]?.updated_at || '').localeCompare(String(a[1]?.updated_at || '')))
                .slice(0, 12);
            writeModelOptionsCache(Object.fromEntries(entries));
        };

        const loadModelOptionsCache = (provider = null) => {
            const entry = readModelOptionsCache()[modelCacheProviderKey(provider)];
            if (!entry?.options) return false;
            const nextOptions = { ...modelOptions.value };
            modelConfigFields.forEach(field => {
                const cached = entry.options[field.capability] || [];
                if (Array.isArray(cached) && cached.length) {
                    nextOptions[field.capability] = mergeModelOptions(cached);
                }
            });
            modelOptions.value = nextOptions;
            return true;
        };

        const removeModelOptionsCacheByProviderId = (providerId) => {
            const targetId = (providerId || '').trim();
            if (!targetId) return;
            const cache = readModelOptionsCache();
            let changed = false;
            Object.keys(cache).forEach(key => {
                if (key.startsWith(`${targetId}|`) || cache[key]?.provider?.id === targetId) {
                    delete cache[key];
                    changed = true;
                }
            });
            if (changed) writeModelOptionsCache(cache);
        };

        const syncConfigModelOptions = (models, { provider = null, capabilities = null } = {}) => {
            const baseModels = mergeModelOptions(models || []);
            const allowed = capabilities ? new Set(capabilities) : null;
            const providerInfo = provider || activeAiProvider.value || {
                id: activeProviderId.value,
                name: activeProviderName.value,
            };
            const nextOptions = { ...modelOptions.value };
            modelConfigFields.forEach(field => {
                if (allowed && !allowed.has(field.capability)) return;
                const currentModel = (apiKeyInputs.value[field.key] || '').trim();
                nextOptions[field.capability] = ensureModelOption(baseModels, currentModel, providerInfo);
            });
            modelOptions.value = nextOptions;
        };

        const openModelDropdown = (capability) => {
            if ((modelOptions.value[capability] || []).length) {
                openModelDropdownCapability.value = capability;
            }
        };

        const toggleModelDropdown = (capability) => {
            openModelDropdownCapability.value = openModelDropdownCapability.value === capability ? '' : capability;
        };

        const filteredConfigModels = (capability) => {
            const q = (dropdownModelSearches.value[capability] || '').trim().toLowerCase();
            const models = modelOptions.value[capability] || [];
            if (!q) return models;
            return models.filter(m => `${m.label || ''} ${m.key || ''} ${m.model_id || ''}`.toLowerCase().includes(q));
        };

        const selectFirstFilteredConfigModel = (field) => {
            const first = filteredConfigModels(field.capability)[0];
            if (first) selectConfigModel(field.key, first.key, field.capability);
        };

        const configModelRequestBody = (capability) => ({
            capability,
            provider_id: apiKeyInputs.value.providerId || '',
            provider_name: apiKeyInputs.value.providerName || '',
            openai_compat_api_key: apiKeyInputs.value.openaiApiKey || '',
            openai_compat_base_url: apiKeyInputs.value.baseUrl || '',
            chat_model: apiKeyInputs.value.chatModel || '',
            embedding_model: apiKeyInputs.value.embeddingModel || '',
            image_model: apiKeyInputs.value.imageModel || '',
            campaign_model: apiKeyInputs.value.campaignModel || '',
        });

        const requestConfigModels = async (capability) => {
            const r = await fetch(`${API_BASE_URL}/api/config/models`, {
                method: 'POST',
                headers: configRequestHeaders(),
                body: JSON.stringify(configModelRequestBody(capability)),
            });
            return await r.json();
        };

        const fetchConfigModels = async (capability = 'chat') => {
            if (isFetchingConfigModels.value) return;
            if (!requireProviderConfigUi()) return;
            isFetchingConfigModels.value = true;
            fetchingConfigCapability.value = capability;
            apiKeySaveMsg.value = '';
            try {
                const d = await requestConfigModels(capability);
                if (d.status === 'success') {
                    const field = modelConfigFields.find(f => f.capability === capability);
                    const currentModel = field ? (apiKeyInputs.value[field.key] || '') : '';
                    const models = ensureModelOption(d.models || [], currentModel, d.provider);
                    modelOptions.value = {
                        ...modelOptions.value,
                        [capability]: models
                    };
                    syncConfigModelOptions(models, { provider: d.provider, capabilities: [capability] });
                    saveModelOptionsCache(d.provider);
                    dropdownModelSearches.value = { ...dropdownModelSearches.value, [capability]: '' };
                    if (field?.key === 'chatModel' && apiKeyInputs.value.chatModel) {
                        syncChatModelSelection(apiKeyInputs.value.chatModel, { models, provider: d.provider });
                    }
                    if (d.provider && !d.draft) {
                        activeAiProvider.value = {
                            id: d.provider.id,
                            name: d.provider.name,
                            base_url: d.provider.base_url,
                        };
                    }
                    if (d.error) {
                        apiKeySaveOk.value = false;
                        apiKeySaveMsg.value = d.error;
                    }
                } else {
                    apiKeySaveOk.value = false;
                    apiKeySaveMsg.value = d.message || '获取模型失败';
                }
            } catch(e) {
                apiKeySaveOk.value = false;
                apiKeySaveMsg.value = '获取模型失败';
            } finally {
                isFetchingConfigModels.value = false;
                fetchingConfigCapability.value = '';
            }
        };

        const fetchAllConfigModels = async () => {
            if (isFetchingConfigModels.value) return;
            if (!requireProviderConfigUi()) return;
            isFetchingConfigModels.value = true;
            fetchingConfigCapability.value = 'all';
            apiKeySaveMsg.value = '';
            try {
                const results = await Promise.all(modelConfigFields.map(async field => {
                    const d = await requestConfigModels(field.capability);
                    if (d.status !== 'success') {
                        apiKeySaveOk.value = false;
                        apiKeySaveMsg.value = d.message || '获取模型失败';
                    }
                    return { field, data: d };
                }));
                const allModels = mergeModelOptions(...results.map(result => result.data?.models || []));
                const nextOptions = { ...modelOptions.value };
                const nextDropdownSearches = { ...dropdownModelSearches.value };
                results.forEach(({ field, data }) => {
                    if (data?.status !== 'success') return;
                    const currentModel = apiKeyInputs.value[field.key] || '';
                    nextOptions[field.capability] = ensureModelOption(allModels, currentModel, data.provider);
                    nextDropdownSearches[field.capability] = '';
                    if (field.key === 'chatModel' && apiKeyInputs.value.chatModel) {
                        syncChatModelSelection(apiKeyInputs.value.chatModel, {
                            models: nextOptions[field.capability],
                            provider: data.provider
                        });
                    }
                    if (data.provider && !data.draft) {
                        activeAiProvider.value = {
                            id: data.provider.id,
                            name: data.provider.name,
                            base_url: data.provider.base_url,
                        };
                    }
                });
                modelOptions.value = nextOptions;
                syncConfigModelOptions(allModels, { provider: activeAiProvider.value });
                saveModelOptionsCache(activeAiProvider.value);
                dropdownModelSearches.value = nextDropdownSearches;
                if (!apiKeySaveMsg.value) {
                    apiKeySaveOk.value = true;
                    apiKeySaveMsg.value = '模型列表已更新';
                    setTimeout(() => { apiKeySaveMsg.value = ''; }, 2500);
                }
            } catch(e) {
                apiKeySaveOk.value = false;
                apiKeySaveMsg.value = '获取模型失败';
            } finally {
                isFetchingConfigModels.value = false;
                fetchingConfigCapability.value = '';
            }
        };

        const selectConfigModel = async (fieldKey, modelId, capability = '') => {
            if (!modelId) return;
            apiKeyInputs.value[fieldKey] = modelId;
            if (fieldKey === 'chatModel') syncChatModelSelection(modelId);
            if (fieldKey === 'imageModel') imgModel.value = modelId;
            if (capability) openModelDropdownCapability.value = '';
        };

        const saveApiKeys = async () => {
            if (!requireProviderConfigUi()) return;
            isSavingKeys.value = true;
            apiKeySaveMsg.value = '';
            try {
                const r = await fetch(`${API_BASE_URL}/api/config/keys`, {
                    method: 'POST',
                    headers: configRequestHeaders(),
                    body: JSON.stringify({
                        provider_id: apiKeyInputs.value.providerId || null,
                        provider_name: apiKeyInputs.value.providerName || `Provider ${apiProviders.value.length + 1}`,
                        openai_compat_api_key: apiKeyInputs.value.openaiApiKey || null,
                        openai_compat_base_url: apiKeyInputs.value.baseUrl || null,
                        chat_model: apiKeyInputs.value.chatModel || null,
                        embedding_model: apiKeyInputs.value.embeddingModel || null,
                        image_model: apiKeyInputs.value.imageModel || null,
                        campaign_model: apiKeyInputs.value.campaignModel || null,
                        image_size: apiKeyInputs.value.imageSize || null,
                        token_policy_mode: tokenPolicy.value.mode || 'full',
                        make_active: true,
                    })
                });
                const d = await r.json();
                if (d.status === 'success') {
                    apiKeySaveOk.value = true;
                    apiKeySaveMsg.value = d.message || '已保存';
                    apiKeyInputs.value.openaiApiKey = '';
                    applyApiConfigPayload(d);
                    syncConfiguredModelState();
                    saveModelOptionsCache(activeAiProvider.value);
                    setTimeout(() => { apiKeySaveMsg.value = ''; }, 3000);
                } else {
                    apiKeySaveOk.value = false;
                    apiKeySaveMsg.value = d.message || '保存失败';
                }
            } catch(e) {
                apiKeySaveOk.value = false;
                apiKeySaveMsg.value = '网络错误';
            } finally {
                isSavingKeys.value = false;
            }
        };
        const selectedCampaign = ref('');
        const selectedCampaignInfo = computed(() => {
            return campaignFiles.value.find(f => f.path === selectedCampaign.value) || null;
        });
        const normalizeCampaignPath = (value = '') => String(value || '').trim().replace(/\\/g, '/').replace(/^\/+/, '');
        const activeSoloSessionId = ref('');
        const loadedCampaignPath = ref('');
        const loadedCampaignName = ref('');
        const loadedBaseCampaignPath = ref('');
        const loadedBaseCampaignName = ref('');
        const currentSavePath = ref('');
        const campaignSelectionMatches = (campaign, path) => {
            const normalized = normalizeCampaignPath(path);
            if (!campaign || !normalized) return false;
            const campaignPath = normalizeCampaignPath(campaign.path);
            const campaignName = normalizeCampaignPath(campaign.name);
            return (
                campaignPath === normalized ||
                campaignName === normalized ||
                campaignPath === `campaigns/${normalized}`
            );
        };
        // AI 模型选择
        const aiModels = ref([]);
        const aiModelSearch = ref('');
        const activeAiModel = ref('');
        const aiModelDraft = ref('');
        const openAiModelDropdownState = ref(false);
        const isFetchingAiModels = ref(false);
        const aiModelError = ref('');
        const syncChatModelSelection = (modelKey, { models = null, provider = null } = {}) => {
            const key = (modelKey || '').trim();
            activeAiModel.value = key;
            aiModelDraft.value = key;
            if (apiKeyInputs.value.chatModel !== key) {
                apiKeyInputs.value = { ...apiKeyInputs.value, chatModel: key };
            }
            if (!key) return;
            const providerInfo = provider || activeAiProvider.value || {
                id: activeProviderId.value,
                name: activeProviderName.value,
            };
            const remoteModels = Array.isArray(models) && models.length ? models : aiModels.value;
            aiModels.value = ensureModelOption(remoteModels, key, providerInfo);
            const chatOptions = (modelOptions.value.chat || []).length ? modelOptions.value.chat : aiModels.value;
            modelOptions.value = {
                ...modelOptions.value,
                chat: ensureModelOption(chatOptions, key, providerInfo)
            };
        };
        const syncConfiguredModelState = () => {
            const providerInfo = activeAiProvider.value || {
                id: activeProviderId.value,
                name: activeProviderName.value,
            };
            const chatModel = (apiKeyInputs.value.chatModel || '').trim();
            if (chatModel) {
                const existingChatOptions = (modelOptions.value.chat || []).length ? modelOptions.value.chat : aiModels.value;
                syncChatModelSelection(chatModel, { models: existingChatOptions, provider: providerInfo });
            } else {
                activeAiModel.value = '';
                aiModelDraft.value = '';
                if (!(modelOptions.value.chat || []).length) aiModels.value = [];
            }
            const preserveOptions = (capability, fieldKey) => {
                const currentOptions = modelOptions.value[capability] || [];
                const model = (apiKeyInputs.value[fieldKey] || '').trim();
                return model ? ensureModelOption(currentOptions, model, providerInfo) : currentOptions;
            };
            modelOptions.value = {
                ...modelOptions.value,
                embedding: preserveOptions('embedding', 'embeddingModel'),
                image: preserveOptions('image', 'imageModel'),
                campaign: preserveOptions('campaign', 'campaignModel')
            };
            aiModelError.value = '';
            openAiModelDropdownState.value = false;
        };
        const storyNodes = ref([]);
        const characters = ref([]);
        const currentNode = ref(null);
        const isEditMode = ref(false);
        const hpLabel = ref('HP');
        const sanLabel = ref('SAN');
        const editData = ref({ name:'', summary:'', content:'' });
        const newOptionText = ref('');
        const newOptionTarget = ref('');
        const showWorldviewModal = ref(false);
        const worldviewContent = ref('');
        const showMemoryModal = ref(false);
        const memoryContent = ref('');
        const showLorebookModal = ref(false);
        const lorebook = ref([]);
        const currentLore = ref({keywords:'', content:''});
        const aiGeneratedText = ref({});
        const playerAction = ref('');
        const actionType = ref('mixed');  // 动作经济分离：'dialogue' | 'action' | 'mixed'
        const savedSoloProfile = readStoredJson('zric-solo-profile', {});
        const normalizeSoloCharacterSelection = (ids) => {
            const list = Array.isArray(ids) ? ids : (ids ? [ids] : []);
            const first = list.find(id => id !== null && id !== undefined && String(id).trim() !== '');
            return first === undefined ? [] : [first];
        };
        const soloPlayerName = ref(savedSoloProfile.name || '玩家');
        const soloSelectedCharacterIds = ref(normalizeSoloCharacterSelection(savedSoloProfile.characterIds));
        const soloConfirmedCharacterIds = ref([]);
        const soloActionLog = ref([]);
        const soloLogRef = ref(null);
        const soloLogSortValue = (entry) => {
            const idTime = Number(String(entry?.id || '').split('-')[0]);
            if (Number.isFinite(idTime) && idTime > 0) return idTime;
            const parsed = Date.parse(entry?.created_at || entry?.time || '');
            return Number.isFinite(parsed) ? parsed : 0;
        };
        const visibleSoloActionLog = computed(() =>
            [...soloActionLog.value].sort((a, b) => soloLogSortValue(b) - soloLogSortValue(a))
        );
        const isGeneratingText = ref(false);
        const checkpointCount = ref(0);
        const isRollingBack = ref(false);
        const isGeneratingOptions = ref(false);

        // GM 干预/纠正推演
        const gmCorrection = ref('');
        const lastDynamicContext = ref(null);  // {action, sceneName, content, nodeId, branchCount}
        const gmManualEventText = ref('');
        const gmManualEventKind = ref('ai');
        const gmManualEventBusy = ref(false);
        const gmManualEventMsg = ref('');
        const gmManualSyncPlayer = ref(true);
        const gmManualRecordMemory = ref(true);
        // NPC Persona 编辑模态
        const showPersonaModal = ref(false);
        const personaTarget = ref(null);  // 当前正在编辑的角色对象
        const personaMbti = ref('');
        const personaQuirks = ref([]);
        const personaNote = ref('');
        const mbtiPool = [
            'INTJ(绝对理智/防御性强)',
            'INTP(分析痴迷/社交笨拙/活在脑海里)',
            'ENTJ(强势/目标导向/控制欲)',
            'ENTP(诡辩/好奇/挑衅式对话)',
            'INFJ(洞察人心/理想主义/极少流露真情)',
            'INFP(敏感/共情/容易内耗)',
            'ENFJ(魅力领袖/操控式关怀/情绪感染力强)',
            'ENFP(热情/注意力跳跃)',
            'ISTJ(刻板/守序/关注细节)',
            'ISFJ(温和/忠诚/害怕冲突)',
            'ESTJ(实用主义/命令型/不容异议)',
            'ESFJ(讨好型/八卦/极度在意他人评价)',
            'ISTP(冷静观察/行动突然/难以捉摸)',
            'ISFP(温柔随性/内心激烈/逃避对抗)',
            'ESTP(行动派/缺乏耐心/轻佻)',
            'ESFP(表演欲强/活在当下/情绪化)',
        ];
        const quirkPool = [
            '说话语速极快',
            '不敢直视对方眼睛',
            '习惯性摆弄手里的物件',
            '一紧张或说谎就会结巴',
            '喜欢用疑问句反问对方',
            '过度礼貌但带有极强的距离感',
            '经常冷笑或自言自语',
            '说话时眼神飘忽不定',
        ];
        const imgPrompt = ref('');
        const imgStyle = ref('none');
        const imgModel = ref('');
        const isGeneratingImage = ref(false);
        const isLoadingImg = ref(false);
        const generatedImageUrl = ref('');
        const imgEnPrompt = ref('');
        const imgPromptUsed = ref('');
        const imgLoadError = ref(false);
        const audioRef = ref(null);
        const isPlaying = ref(false);
        const volume = ref(0.3);
        const currentTrackId = ref(1);
        const tracks = ref([
            { id:1,  name:'午后田园',   url:'https://soundimage.org/wp-content/uploads/2014/08/Netherplace.mp3', custom:false },
            { id:2,  name:'黄昏渡口',   url:'https://soundimage.org/wp-content/uploads/2018/01/Romantic-Lands-Beckon.mp3', custom:false },
            { id:3,  name:'钢琴小品',   url:'https://soundimage.org/wp-content/uploads/2014/04/Ballooning.mp3', custom:false },
            { id:4,  name:'浪漫舞会',   url:'https://soundimage.org/wp-content/uploads/2014/10/Romantic-Halloween-Theme.mp3', custom:false },
            { id:5,  name:'钢琴沉思',   url:'https://soundimage.org/wp-content/uploads/2014/05/Space-for-Thought.mp3', custom:false },
            { id:6,  name:'月光下的林间',   url:'http://soundimage.org/wp-content/uploads/2014/11/Moonlit-Secrets.mp3', custom:false },
            { id:7,  name:'星际穿越',   url:'https://soundimage.org/wp-content/uploads/2025/01/Cyber-Mean-Streets.mp3', custom:false },
            { id:8,  name:'幽影神秘之地',   url:'https://cdn.pixabay.com/audio/2022/08/02/audio_884fe92c21.mp3', custom:false },
            { id:9,  name:'蓝调时刻',   url:'https://cdn.pixabay.com/audio/2022/05/27/audio_1808fbf07a.mp3', custom:false },
            { id:10, name:'赛博梦都',   url:'https://cdn.pixabay.com/audio/2022/11/22/audio_febc508520.mp3', custom:false },
            { id:11, name:'残阳黎明',   url:'https://cdn.pixabay.com/audio/2022/08/23/audio_d16737dc28.mp3', custom:false },
            { id:12, name:'自定义 URL', url:'', custom:true },
        ]);
        <!-- "ID：1-7：Music by Eric Matyas — www.soundimage.org" -->
        const customTrackUrl = ref('');
        const currentTrackUrl = computed(() => {
            const t = tracks.value.find(t=>t.id===currentTrackId.value);
            return t ? (t.custom ? customTrackUrl.value : t.url) : '';
        });
        const currentTrack = computed(() => tracks.value.find(t => t.id === currentTrackId.value) || null);
        const currentTrackName = computed(() => currentTrack.value?.name || '');
        const playerStateCache = ref({});
        const findTrackByName = (name) => {
            const target = String(name || '').trim();
            if (!target) return null;
            return tracks.value.find(t => t.name === target) || null;
        };
        const playCurrentTrack = async () => {
            await nextTick();
            if (!audioRef.value || !currentTrackUrl.value) return false;
            audioRef.value.volume = volume.value;
            try {
                await audioRef.value.play();
                isPlaying.value = true;
                return true;
            } catch (_) {
                isPlaying.value = false;
                return false;
            }
        };
        const applyBgmState = (state = {}, { playIfPlaying = true } = {}) => {
            playerStateCache.value = { ...playerStateCache.value, ...state };
            const namedTrack = findTrackByName(state.bgm_name);
            if (namedTrack) {
                currentTrackId.value = namedTrack.id;
            } else if (state.bgm_url) {
                const urlTrack = tracks.value.find(t => !t.custom && t.url === state.bgm_url);
                if (urlTrack) {
                    currentTrackId.value = urlTrack.id;
                } else {
                    customTrackUrl.value = state.bgm_url;
                    currentTrackId.value = 12;
                }
            }
            if (playIfPlaying && isPlaying.value) playCurrentTrack();
        };
        const applyBgmName = (name, { playIfPlaying = true } = {}) => {
            const track = findTrackByName(name);
            if (!track) return false;
            currentTrackId.value = track.id;
            if (playIfPlaying && isPlaying.value) playCurrentTrack();
            return true;
        };
        const fetchPlayerStateBgm = async (options = {}) => {
            try {
                const r = await fetch(`${API_BASE_URL}/api/player/state`);
                const d = await r.json();
                applyBgmState(d, options);
                return d;
            } catch (_) {
                return null;
            }
        };
        const syncPlayerStateBgm = async (extra = {}) => {
            const nodeId = currentNode.value?.id || playerStateCache.value.current_scene_id || 0;
            const cachedAiText = nodeId ? (aiGeneratedText.value[nodeId] || playerStateCache.value.scene_ai_text || '') : (playerStateCache.value.scene_ai_text || '');
            const payload = {
                current_scene_id: nodeId,
                scene_image: generatedImageUrl.value || playerStateCache.value.scene_image || '',
                scene_prompt: imgPromptUsed.value || playerStateCache.value.scene_prompt || '',
                scene_ai_text: cachedAiText,
                bgm_url: currentTrackUrl.value || '',
                bgm_name: currentTrackName.value || '',
                ...extra,
            };
            try {
                await fetch(`${API_BASE_URL}/api/player/state`, {
                    method: 'POST',
                    headers: accountHeaders(true),
                    body: JSON.stringify(payload),
                });
                playerStateCache.value = { ...playerStateCache.value, ...payload };
            } catch (_) {}
        };
        const refreshCheckpointCount = async () => {
            try {
                const r = await fetch(`${API_BASE_URL}/api/game/checkpoints`);
                const d = await r.json();
                if (d.status === 'success') checkpointCount.value = Number(d.count || 0);
            } catch (_) {}
        };
        const createGameCheckpoint = async (label = '场景推进', nodeId = currentNode.value?.id) => {
            if (!nodeId) return false;
            try {
                const r = await fetch(`${API_BASE_URL}/api/game/checkpoint`, {
                    method: 'POST',
                    headers: accountHeaders(true),
                    body: JSON.stringify({from_node_id: nodeId, label}),
                });
                const d = await r.json().catch(() => ({}));
                if (d.status === 'success') {
                    checkpointCount.value = Number(d.remaining ?? (checkpointCount.value + 1));
                    return true;
                }
            } catch (_) {}
            return false;
        };
        const narrativeMood = ref('');
        const timeSkipInput = ref('');
        const forceNarrativeThrust = ref(false);
        const optionLikelihoods = ref({});
        const fateSpinning = ref(false);
        const fateHighlightIdx = ref(-1);
        const dicePanel = ref({
            expression: '1d100',
            actor_name: 'GM',
            skill_name: '',
            skill_value: null,
            reason: '',
            record_to_memory: true
        });
        const diceBusy = ref(false);
        const diceError = ref('');
        const diceResult = ref(null);
        const diceResultText = computed(() => {
            const result = diceResult.value;
            if (!result) return '';
            if (result.summary) return result.summary;
            if (result.result) return result.result;
            if (result.outcome) return result.outcome;
            return JSON.stringify(result, null, 2);
        });

        // ── 多人房间：复用 /api/multiplayer 与 /ws/rooms ──
        const savedMpProfile = readStoredJson('zric-mp-profile', {});
        const savedMpTokens = readStoredJson('zric-mp-tokens', {});
        const savedAuthAccount = readStoredJson('zric-auth-account', null);
        const multiplayerClientId = readOrCreateMultiplayerClientId();
        const showMultiplayerModal = ref(false);
        const multiplayerRoom = ref(null);
        const multiplayerMembers = ref([]);
        const multiplayerMessages = ref([]);
        const multiplayerBusy = ref(false);
        const multiplayerError = ref('');
        const multiplayerStatusMsg = ref('');
        const multiplayerWsConnected = ref(false);
        const multiplayerRoomName = ref('新的跑团房间');
        const multiplayerMaxPlayers = ref(0);
        const multiplayerJoinCode = ref('');
        const multiplayerProfileName = ref(savedMpProfile.name && savedMpProfile.name !== 'GM' ? savedMpProfile.name : '玩家');
        const multiplayerPlayerId = ref(savedMpProfile.id || `player-${Math.random().toString(36).slice(2, 10)}`);
        const multiplayerRoomTokens = ref(savedMpTokens.roomTokens || {});
        const multiplayerMemberTokens = ref(savedMpTokens.memberTokens || {});
        const multiplayerAuthToken = ref(localStorage.getItem('zric-auth-token') || '');
        const multiplayerAuthAccount = ref(savedAuthAccount);
        const multiplayerAuthForm = ref({ username: '', password: '', display_name: '' });
        const multiplayerAuthBusy = ref(false);
        const multiplayerAuthMsg = ref('');
        let multiplayerWs = null;
        const MULTIPLAYER_MAX_ROOM_PLAYERS = 24;

        const multiplayerPlayableCharacterLimit = computed(() => {
            const selectedLimit = Number(selectedCampaignInfo.value?.playable_character_count || selectedCampaignInfo.value?.character_count || 0);
            if (Number.isFinite(selectedLimit) && selectedLimit > 0) {
                return Math.max(1, selectedLimit);
            }
            const active = characters.value.filter(c => (c.status || 'active') !== 'hidden');
            return Math.max(1, active.length || characters.value.length || 1);
        });
        const multiplayerMaxPlayersUpper = computed(() => {
            const selectedMax = Number(selectedCampaignInfo.value?.max_players || 0);
            const upper = Number.isFinite(selectedMax) && selectedMax > 0
                ? selectedMax
                : multiplayerPlayableCharacterLimit.value;
            return Math.max(1, Math.min(upper, MULTIPLAYER_MAX_ROOM_PLAYERS));
        });
        const clampMultiplayerMaxPlayers = () => {
            const upper = multiplayerMaxPlayersUpper.value;
            let value = Number.parseInt(multiplayerMaxPlayers.value, 10);
            if (!Number.isFinite(value)) value = upper;
            multiplayerMaxPlayers.value = Math.max(1, Math.min(value, upper));
        };
        watch(multiplayerMaxPlayersUpper, (upper, previousUpper) => {
            const current = Number.parseInt(multiplayerMaxPlayers.value, 10);
            if (!Number.isFinite(current) || current < 1 || current > upper || current === previousUpper) {
                multiplayerMaxPlayers.value = upper;
            }
        }, { immediate: true });
        const multiplayerRoomMaxPlayers = computed(() => {
            return Number(multiplayerRoom.value?.max_players || multiplayerRoom.value?.settings?.max_players || multiplayerMaxPlayers.value || 1);
        });
        const multiplayerClaimedPlayerCount = computed(() => Number(multiplayerRoom.value?.claimed_character_count ?? multiplayerRoom.value?.claimed_player_count ?? 0));
        const multiplayerJoinedPlayerCount = computed(() => Number(multiplayerRoom.value?.joined_player_count ?? multiplayerMembers.value.length ?? 0));
        const multiplayerRoomSeatsRemaining = computed(() => {
            const fromRoom = Number(multiplayerRoom.value?.player_slots_remaining);
            if (Number.isFinite(fromRoom)) return Math.max(0, fromRoom);
            return Math.max(0, multiplayerRoomMaxPlayers.value - multiplayerJoinedPlayerCount.value);
        });

        const multiplayerTableUrl = (code) => {
            if (!code) return '';
            const origin = window.location.protocol.startsWith('http') ? window.location.origin : API_BASE_URL;
            return `${origin}/multiplayer.html?room=${encodeURIComponent(code)}`;
        };
        const multiplayerInviteUrl = computed(() => {
            if (!multiplayerRoom.value?.code) return '';
            return multiplayerTableUrl(multiplayerRoom.value.code);
        });

        const applyMultiplayerAuthSession = (data) => {
            const account = data?.account;
            if (!account?.player_id) return;
            multiplayerAuthAccount.value = account;
            if (data.token) {
                multiplayerAuthToken.value = data.token;
                localStorage.setItem('zric-auth-token', data.token);
            }
            localStorage.setItem('zric-auth-account', JSON.stringify(account));
            multiplayerPlayerId.value = account.player_id;
            multiplayerProfileName.value = account.display_name || account.username || '玩家';
            saveMultiplayerLocalState();
        };

        const submitMultiplayerAuth = async (mode = 'login') => {
            multiplayerAuthBusy.value = true;
            multiplayerAuthMsg.value = '';
            try {
                const r = await fetch(`${API_BASE_URL}/api/auth/${mode === 'register' ? 'register' : 'login'}`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(multiplayerAuthForm.value),
                });
                const data = await r.json().catch(() => ({}));
                if (!r.ok || data.status === 'error') throw new Error(data.detail || data.message || '登录失败');
                applyMultiplayerAuthSession(data);
                multiplayerAuthForm.value = { ...multiplayerAuthForm.value, password: '' };
                multiplayerAuthMsg.value = '账号已登录';
                await fetchApiKeyStatus({ resetModels: true });
                await fetchCampaigns();
                await fetchSaves();
            } catch (e) {
                multiplayerAuthMsg.value = e.message || '登录失败';
            } finally {
                multiplayerAuthBusy.value = false;
            }
        };

        const logoutMultiplayerAuth = async () => {
            multiplayerAuthBusy.value = true;
            try {
                if (multiplayerAuthToken.value) {
                    await fetch(`${API_BASE_URL}/api/auth/logout`, { method: 'POST', headers: {'X-Auth-Token': multiplayerAuthToken.value} });
                }
            } catch (_) {
            } finally {
                multiplayerAuthToken.value = '';
                multiplayerAuthAccount.value = null;
                multiplayerMemberTokens.value = {};
                localStorage.removeItem('zric-auth-token');
                localStorage.removeItem('zric-auth-account');
                localStorage.setItem('zric-mp-tokens', JSON.stringify({ roomTokens: multiplayerRoomTokens.value, memberTokens: {} }));
                if (multiplayerWs) multiplayerWs.close();
                multiplayerAuthMsg.value = '账号已退出';
                await fetchApiKeyStatus({ resetModels: true });
                await fetchCampaigns();
                await fetchSaves();
                multiplayerAuthBusy.value = false;
            }
        };

        const requireMultiplayerAuth = () => {
            if (multiplayerAuthAccount.value && multiplayerAuthToken.value) return true;
            multiplayerAuthMsg.value = '请先登录账号';
            return false;
        };
        const accountHeaders = (jsonBody = false) => {
            const headers = jsonBody ? {'Content-Type': 'application/json'} : {};
            if (multiplayerAuthToken.value) headers['X-Auth-Token'] = multiplayerAuthToken.value;
            return headers;
        };

        const refreshMultiplayerAuthAccount = async () => {
            if (!multiplayerAuthToken.value) return;
            try {
                const r = await fetch(`${API_BASE_URL}/api/auth/me`, { headers: {'X-Auth-Token': multiplayerAuthToken.value} });
                const data = await r.json().catch(() => ({}));
                if (!r.ok || data.status === 'error') throw new Error(data.detail || data.message || '账号状态失效');
                applyMultiplayerAuthSession(data);
            } catch (_) {
                multiplayerAuthToken.value = '';
                multiplayerAuthAccount.value = null;
                localStorage.removeItem('zric-auth-token');
                localStorage.removeItem('zric-auth-account');
            }
        };

        const saveMultiplayerLocalState = () => {
            localStorage.setItem('zric-mp-client-id', multiplayerClientId);
            localStorage.setItem('zric-mp-profile', JSON.stringify({
                id: multiplayerPlayerId.value,
                name: multiplayerProfileName.value || '玩家',
                role: 'player',
                color: '#7dd3fc',
                client_id: multiplayerClientId,
            }));
            localStorage.setItem('zric-mp-tokens', JSON.stringify({
                roomTokens: multiplayerRoomTokens.value,
                memberTokens: multiplayerMemberTokens.value,
            }));
        };

        const multiplayerExtractRoomCode = (path) => {
            const match = String(path || '').match(/\/api\/multiplayer\/rooms\/([^/?]+)/);
            return match ? decodeURIComponent(match[1]) : '';
        };

        const multiplayerHeaders = (path, jsonBody = true) => {
            const headers = jsonBody ? {'Content-Type': 'application/json'} : {};
            const code = multiplayerRoom.value?.code || multiplayerExtractRoomCode(path);
            if (multiplayerAuthToken.value) headers['X-Auth-Token'] = multiplayerAuthToken.value;
            if (code && multiplayerRoomTokens.value[code]) headers['X-Room-Token'] = multiplayerRoomTokens.value[code];
            if (code && multiplayerMemberTokens.value[code]) headers['X-Member-Token'] = multiplayerMemberTokens.value[code];
            return headers;
        };

        const multiplayerApi = async (path, options = {}) => {
            const jsonBody = !(options.body instanceof FormData);
            const r = await fetch(`${API_BASE_URL}${path}`, {
                ...options,
                headers: { ...multiplayerHeaders(path, jsonBody), ...(options.headers || {}) },
            });
            const data = await r.json().catch(() => ({}));
            if (!r.ok || data.status === 'error') throw new Error(data.detail || data.message || '多人房间请求失败');
            return data;
        };

        const syncMultiplayerMember = (data) => {
            const member = data?.member;
            if (!member?.player_id) return;
            multiplayerPlayerId.value = member.player_id;
            multiplayerProfileName.value = member.display_name || multiplayerProfileName.value || '玩家';
            saveMultiplayerLocalState();
        };

        const applyMultiplayerSnapshot = (data) => {
            syncMultiplayerMember(data);
            if (data.room) {
                multiplayerRoom.value = data.room;
                multiplayerJoinCode.value = data.room.code || multiplayerJoinCode.value;
                multiplayerRoomName.value = data.room.name || multiplayerRoomName.value;
                const roomMax = Number(data.room.max_players || data.room.settings?.max_players);
                if (Number.isFinite(roomMax) && roomMax > 0) multiplayerMaxPlayers.value = roomMax;
            }
            multiplayerMembers.value = data.members || multiplayerMembers.value;
            multiplayerMessages.value = data.messages || multiplayerMessages.value;
        };

        const connectMultiplayerWs = () => {
            if (!multiplayerRoom.value?.code || !window.location.protocol.startsWith('http')) return;
            if (multiplayerWs) multiplayerWs.close();
            const code = multiplayerRoom.value.code;
            const params = new URLSearchParams({
                player_id: multiplayerPlayerId.value,
                name: multiplayerProfileName.value || '玩家',
                role: 'player',
                client_id: multiplayerClientId,
                auth_token: multiplayerAuthToken.value,
            });
            if (multiplayerMemberTokens.value[code]) params.set('member_token', multiplayerMemberTokens.value[code]);
            const wsUrl = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/rooms/${code}?${params}`;
            multiplayerWs = new WebSocket(wsUrl);
            multiplayerWs.onopen = () => { multiplayerWsConnected.value = true; };
            multiplayerWs.onclose = () => { multiplayerWsConnected.value = false; };
            multiplayerWs.onerror = () => { multiplayerWsConnected.value = false; };
            multiplayerWs.onmessage = (event) => {
                const evt = JSON.parse(event.data);
                if (evt.member_token && evt.member) syncMultiplayerMember(evt);
                if (evt.member_token && evt.room?.code) {
                    multiplayerMemberTokens.value = { ...multiplayerMemberTokens.value, [evt.room.code]: evt.member_token };
                    saveMultiplayerLocalState();
                }
                if (evt.type === 'snapshot') applyMultiplayerSnapshot(evt);
                else if (evt.type === 'member.updated') {
                    const idx = multiplayerMembers.value.findIndex(m => m.player_id === evt.member.player_id);
                    if (idx >= 0) multiplayerMembers.value[idx] = evt.member;
                    else multiplayerMembers.value.push(evt.member);
                } else if (evt.type === 'member.left') {
                    const member = multiplayerMembers.value.find(m => m.player_id === evt.player_id);
                    if (member) member.connected = 0;
                } else if (evt.type === 'message.created') {
                    if (!multiplayerMessages.value.some(m => m.id === evt.message.id)) multiplayerMessages.value.push(evt.message);
                } else if (evt.type === 'game.state') {
                    if (evt.game_state?.characters) characters.value = evt.game_state.characters;
                }
            };
        };

        const syncMultiplayerRoomState = async () => {
            const room = multiplayerRoom.value;
            if (!room?.code || !multiplayerRoomTokens.value[room.code]) return;
            try {
                const activeMapRoom = (typeof mapRooms !== 'undefined' && mapRooms.value || []).find(r => r.state === 'active');
                const currentSettings = room.settings && typeof room.settings === 'object' ? room.settings : {};
                const data = await multiplayerApi(`/api/multiplayer/rooms/${encodeURIComponent(room.code)}`, {
                    method: 'PATCH',
                    body: JSON.stringify({
                        current_scene_id: currentNode.value?.id || null,
                        current_room_id: activeMapRoom?.id || null,
                        settings: {
                            ...currentSettings,
                            gm_console: true,
                            campaign_path: selectedCampaign.value || loadedCampaignPath.value || currentSettings.campaign_path || '',
                        },
                    }),
                });
                applyMultiplayerSnapshot(data);
            } catch(e) {}
        };

        const postMultiplayerAiEvent = async (kind, content, payload = {}, options = {}) => {
            const room = multiplayerRoom.value;
            if (!room?.code || !multiplayerRoomTokens.value[room.code]) return;
            const text = (content || '').trim();
            if (!text) return;
            try {
                await syncMultiplayerRoomState();
                const data = await multiplayerApi(`/api/multiplayer/rooms/${encodeURIComponent(room.code)}/events`, {
                    method: 'POST',
                    body: JSON.stringify({
                        kind,
                        content: text,
                        payload,
                        sender_name: options.senderName || 'AI-KP',
                        broadcast_state: options.broadcastState !== false,
                    }),
                });
                applyMultiplayerSnapshot(data);
            } catch(e) {}
        };

        const createMultiplayerRoom = async (options = {}) => {
            if (!requireMultiplayerAuth()) return;
            multiplayerBusy.value = true;
            multiplayerError.value = '';
            multiplayerStatusMsg.value = '';
            clampMultiplayerMaxPlayers();
            saveMultiplayerLocalState();
            try {
                const roomSavePath = normalizeCampaignPath(options.roomSavePath || '');
                const campaignPath = normalizeCampaignPath(
                    options.campaignPath
                    || selectedCampaign.value
                    || loadedBaseCampaignPath.value
                    || loadedCampaignPath.value
                    || currentSavePath.value
                    || ''
                );
                const settings = {
                    gm_console: true,
                    max_players: multiplayerMaxPlayers.value,
                };
                if (campaignPath) settings.campaign_path = campaignPath;
                if (roomSavePath) settings.room_save_path = roomSavePath;
                if (options.restoreState && typeof options.restoreState === 'object') {
                    settings.restore_state = options.restoreState;
                }
                const data = await multiplayerApi('/api/multiplayer/rooms', {
                    method: 'POST',
                    body: JSON.stringify({
                        name: options.name || multiplayerRoomName.value || currentSaveFolder.value || '新的跑团房间',
                        gm_name: 'AI-GM',
                        campaign_path: campaignPath,
                        restore_code: options.restoreCode || '',
                        settings,
                    }),
                });
                if (data.room?.code && data.room_token) {
                    multiplayerRoomTokens.value = { ...multiplayerRoomTokens.value, [data.room.code]: data.room_token };
                    saveMultiplayerLocalState();
                }
                applyMultiplayerSnapshot(data);
                if (appState.value === 'game') await syncMultiplayerRoomState();
                multiplayerStatusMsg.value = `房间 ${data.room.code} 已创建`;
                return data;
            } catch(e) {
                multiplayerError.value = e.message || '创建房间失败';
                return null;
            } finally {
                multiplayerBusy.value = false;
            }
        };

        const joinMultiplayerRoomData = async (code, roleOverride = '') => {
            const data = await multiplayerApi(`/api/multiplayer/rooms/${encodeURIComponent(code)}/join`, {
                method: 'POST',
                body: JSON.stringify({
                    player_id: multiplayerAuthAccount.value?.player_id || multiplayerPlayerId.value,
                    display_name: multiplayerAuthAccount.value?.display_name || multiplayerProfileName.value || '玩家',
                    role: roleOverride || 'player',
                    color: '#7dd3fc',
                    client_id: multiplayerClientId,
                }),
            });
            syncMultiplayerMember(data);
            if (data.room?.code && data.member_token) {
                multiplayerMemberTokens.value = { ...multiplayerMemberTokens.value, [data.room.code]: data.member_token };
                saveMultiplayerLocalState();
            }
            applyMultiplayerSnapshot(data);
            return data;
        };

        const enterMultiplayerRoomByCode = async (codeArg = '') => {
            const explicitCode = codeArg && typeof codeArg === 'object' && 'type' in codeArg ? '' : codeArg;
            const code = String(explicitCode || multiplayerJoinCode.value || '').trim().toUpperCase();
            if (!code) return;
            if (!requireMultiplayerAuth()) return;
            multiplayerBusy.value = true;
            multiplayerError.value = '';
            multiplayerStatusMsg.value = '';
            saveMultiplayerLocalState();
            try {
                const data = await joinMultiplayerRoomData(code);
                const roomCode = data.room?.code || code;
                multiplayerJoinCode.value = roomCode;
                multiplayerStatusMsg.value = `正在进入房间 ${roomCode}`;
                window.location.href = multiplayerTableUrl(roomCode);
            } catch(e) {
                multiplayerError.value = e.message || '加入房间失败';
            } finally {
                multiplayerBusy.value = false;
            }
        };

        const joinMultiplayerRoom = async (codeArg = '', roleOverride = '') => {
            const explicitCode = codeArg && typeof codeArg === 'object' && 'type' in codeArg ? '' : codeArg;
            const code = String(explicitCode || multiplayerJoinCode.value || '').trim().toUpperCase();
            if (!code) return;
            if (!requireMultiplayerAuth()) return;
            multiplayerBusy.value = true;
            multiplayerError.value = '';
            multiplayerStatusMsg.value = '';
            saveMultiplayerLocalState();
            try {
                const data = await joinMultiplayerRoomData(code, roleOverride);
                connectMultiplayerWs();
                await syncMultiplayerRoomState();
                multiplayerStatusMsg.value = `已加入房间 ${data.room.code}`;
            } catch(e) {
                multiplayerError.value = e.message || '加入房间失败';
            } finally {
                multiplayerBusy.value = false;
            }
        };

        const openMultiplayerModal = () => {
            multiplayerError.value = '';
            multiplayerStatusMsg.value = '';
            showMultiplayerModal.value = true;
        };
        const openMenuMultiplayer = () => {
            multiplayerRoomName.value = selectedCampaignInfo.value
                ? `${(selectedCampaignInfo.value.name || '新的跑团房间')}`
                : (multiplayerRoomName.value || '新的跑团房间');
            clampMultiplayerMaxPlayers();
            openMultiplayerModal();
        };
        const createMultiplayerRoomForSelectedCampaign = async () => {
            if (appState.value === 'menu' && selectedCampaign.value) {
                clampMultiplayerMaxPlayers();
                await loadAndStart('multiplayer', { activate: false, preserveMultiplayerState: true });
                if (campaignLoadSummary.value && campaignLoadSummary.value.includes('失败')) return;
            }
            await createMultiplayerRoom();
        };

        const copyMultiplayerInvite = async () => {
            if (!multiplayerInviteUrl.value) return;
            try {
                await navigator.clipboard.writeText(multiplayerInviteUrl.value);
                multiplayerStatusMsg.value = '邀请链接已复制';
            } catch(e) {
                multiplayerStatusMsg.value = `邀请链接：${multiplayerInviteUrl.value}`;
            }
        };

        const openMultiplayerTable = () => {
            if (!multiplayerInviteUrl.value) return;
            window.open(multiplayerInviteUrl.value, '_blank', 'noopener');
        };
        const showTriggerModal = ref(false);
        const triggers = ref([]);
        const currentTrigger = ref(null);
        const condTypes = [
            { value:'scene', icon:'ph-map-pin',   label:'到达特定场景' },
            { value:'item',  icon:'ph-backpack',   label:'背包含关键物品' },
            { value:'stat',  icon:'ph-heartbeat',  label:'HP/SAN 阈值' },
            { value:'ai',    icon:'ph-robot',      label:'AI 智能判断' },
        ];
        const collapsedChars = ref({});
        const toggleCharCollapse = (id) => { collapsedChars.value[id] = !collapsedChars.value[id]; };
        const showCharModal = ref(false);
        const isGeneratingNPC = ref(false);
        const isExpandingBranch = ref(false);
        const expandingBranchText = ref('');
        const newChar = ref({ name:'', role:'PC', hp:100, san:80, inventory:'' });
        const resetNewChar = () => { newChar.value = { name:'', role:'PC', hp:100, san:80, inventory:'' }; };
        const npcToast = ref(null);
        let npcToastTimer = null;
        const showNpcToast = (npc) => { npcToast.value = npc; if(npcToastTimer) clearTimeout(npcToastTimer); npcToastTimer = setTimeout(()=>{npcToast.value=null;}, 6000); };
        const triggerAlert = ref(null);
        let triggerAlertTimer = null;
        const passiveAlerts = ref([]);
        let passiveAlertTimer = null;
        const showTriggerAlert = (fired) => {
            if (fired.mode === 'passive') {
                // 静默触发：右下角小条显示动作摘要
                if (fired.actions_log && fired.actions_log.length) {
                    passiveAlerts.value.push(fired);
                    if (passiveAlertTimer) clearTimeout(passiveAlertTimer);
                    passiveAlertTimer = setTimeout(() => { passiveAlerts.value = []; }, 6000);
                }
                return;
            }
            triggerAlert.value = fired; if(triggerAlertTimer) clearTimeout(triggerAlertTimer);
            // target_node_id=0 时（如 gen_node 触发器）不自动跳转，避免错误跳到节点0
            if(fired.mode==='hard' && fired.target_node_id) { triggerAlertTimer = setTimeout(()=>{ jumpToNode(fired.target_node_id, fired.target_node_name); triggerAlert.value=null; }, 3000); }
            else { triggerAlertTimer = setTimeout(()=>{triggerAlert.value=null;}, 8000); }
        };
        const statChangesLog = ref([]);
        const showStatChanges = ref(false);
        let statChangesTimer = null;
        const showBattleReportModal = ref(false);
        const battleReport = ref('');
        const showExportModal = ref(false);
        const exportShowNameInput = ref(false);
        const exportNewName = ref('');
        const currentSaveFolder = ref('');
        const isExportingSave = ref(false);
        const isRenamingSave = ref(false);
        const renamingSaveName = ref('');
        const renameSaveDraft = ref('');
        const saveManagerMsg = ref('');
        const saveManagerOk = ref(true);
        const saveSearch = ref('');
        const menuSaveModeFilter = ref('all');
        const saveItems = computed(() => saveFiles.value || []);
        const saveIdentity = (save = {}) => normalizeCampaignPath(save.path || save.name || '');
        const savePlayMode = (save = {}) => {
            const mode = String(save.play_mode || '').trim().toLowerCase();
            if (mode === 'solo' || String(save.name || '').startsWith(SOLO_ROOM_SAVE_PREFIX)) return 'solo';
            if (mode === 'multiplayer' || String(save.name || '').startsWith(MULTIPLAYER_ROOM_SAVE_PREFIX)) return 'multiplayer';
            return 'general';
        };
        const savePlayModeLabel = (save = {}) => {
            const mode = savePlayMode(save);
            if (mode === 'solo') return '单人';
            if (mode === 'multiplayer') return '多人';
            return '通用';
        };
        const saveDisplayName = (save = {}) => {
            const raw = String(save.display_name || save.name || '').trim();
            if (raw.startsWith(SOLO_ROOM_SAVE_PREFIX)) return `单人房间 ${raw.slice(SOLO_ROOM_SAVE_PREFIX.length)}`;
            if (raw.startsWith(MULTIPLAYER_ROOM_SAVE_PREFIX)) return `多人房间 ${raw.slice(MULTIPLAYER_ROOM_SAVE_PREFIX.length)}`;
            return raw || save.path || '未命名存档';
        };
        const currentSaveItem = computed(() => {
            const currentPath = normalizeCampaignPath(currentSavePath.value);
            return saveItems.value.find(s => {
                const path = normalizeCampaignPath(s.path);
                return (currentPath && path === currentPath) || (!!currentSaveFolder.value && s.name === currentSaveFolder.value);
            }) || null;
        });
        const currentSaveWritable = computed(() => !!(currentSaveItem.value?.owned_by_me && currentSaveItem.value?.deletable));
        const filteredSaveItems = computed(() => {
            const q = saveSearch.value.trim().toLowerCase();
            const items = saveItems.value;
            if (!q) return items;
            return items.filter(s => `${s.name || ''} ${s.path || ''} ${s.campaign_name || ''} ${s.updated_at || ''}`.toLowerCase().includes(q));
        });
        const menuSaveModeCounts = computed(() => {
            const counts = { all: saveItems.value.length, solo: 0, multiplayer: 0, general: 0 };
            for (const save of saveItems.value) {
                const mode = savePlayMode(save);
                counts[mode] = Number(counts[mode] || 0) + 1;
            }
            return counts;
        });
        const menuSaveItems = computed(() => {
            const mode = menuSaveModeFilter.value;
            return [...saveItems.value]
                .filter(save => mode === 'all' || savePlayMode(save) === mode)
                .sort((a, b) => saveTimestamp(b) - saveTimestamp(a))
                .slice(0, 8);
        });
        const sanitizeRoomToken = (value = '') =>
            String(value || '').trim().replace(/[^a-zA-Z0-9_-]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 64);
        const isHiddenRoomSaveName = (value = '') => String(value || '').trim().startsWith(HIDDEN_ROOM_SAVE_PREFIX);
        const soloRoomSaveName = (sessionId = '') => {
            const clean = sanitizeRoomToken(sessionId);
            return clean ? `${SOLO_ROOM_SAVE_PREFIX}${clean}` : '';
        };
        const multiplayerRoomSaveName = (roomCode = '') => {
            const clean = sanitizeRoomToken(roomCode);
            return clean ? `${MULTIPLAYER_ROOM_SAVE_PREFIX}${clean}` : '';
        };
        const multiplayerRoomCodeFromSave = (save = {}) => {
            const candidates = [
                save.name,
                normalizeCampaignPath(save.path || '').split('/').pop(),
                save.display_name,
            ];
            for (const candidate of candidates) {
                const raw = String(candidate || '').trim();
                if (raw.startsWith(MULTIPLAYER_ROOM_SAVE_PREFIX)) {
                    return sanitizeRoomToken(raw.slice(MULTIPLAYER_ROOM_SAVE_PREFIX.length)).toUpperCase();
                }
            }
            return '';
        };
        const lockedSaveSourceLabel = computed(() => {
            return loadedBaseCampaignName.value
                || selectedCampaignInfo.value?.name
                || normalizeCampaignPath(lockedSaveSourcePath.value).split('/').pop()
                || '当前剧本';
        });
        const lockedSaveSourcePath = computed(() => {
            const selectedPath = normalizeCampaignPath(selectedCampaign.value || '');
            if (selectedPath.startsWith('campaigns/')) return selectedPath;
            if (loadedBaseCampaignPath.value) return normalizeCampaignPath(loadedBaseCampaignPath.value);
            const loadedPath = normalizeCampaignPath(loadedCampaignPath.value || '');
            return loadedPath.startsWith('campaigns/') ? loadedPath : '';
        });
        const isGeneratingReport = ref(false);
        const battleReportFilename = ref('');
        const leftTab = ref('list');
        const treeContainerRef = ref(null);

        // ══════════════════════════════════════════════════════
        // 【多时间线并行】：响应式状态
        // ══════════════════════════════════════════════════════
        const timelinePanelOpen = ref(false);
        const timelines = ref([]);

        // 新建/编辑模态
        const showTlEditModal = ref(false);
        const tlEditData = ref({ id: null, label: '', color: '#5b9cf5', current_node_id: 0, charSet: new Set() });
        const tlColorPresets = ['#5b9cf5','#34d399','#f87171','#fbbf24','#a78bfa','#fb923c','#e879f9','#38bdf8','#4ade80','#f472b6'];

        // 记忆流模态
        const showTlMemoryModal = ref(false);
        const tlMemoryTarget = ref(null);
        const tlMemoryContent = ref('');

        // 独立推演模态
        const showTlDynamicModal = ref(false);
        const tlDynamicTarget = ref(null);
        const tlDynamicAction = ref('');
        const isTlDynamicRunning = ref(false);

        // 汇合模态
        const showMergeModal = ref(false);
        const mergeSource = ref(null);
        const mergeTargetId = ref(0);
        const isMerging = ref(false);

        // 时间线地图位置模态
        const showTlRoomModal = ref(false);
        const tlRoomTarget    = ref(null);
        const tlRoomSelected  = ref(null);

        // ── 工具函数 ──
        const hexToRgb = hex => {
            const r = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex||'#5b9cf5');
            return r ? `${parseInt(r[1],16)},${parseInt(r[2],16)},${parseInt(r[3],16)}` : '91,156,245';
        };

        // ── 时间线 API 方法 ──
        const fetchTimelines = async () => {
            try {
                const r = await fetch(`${API_BASE_URL}/api/timelines`);
                const d = await r.json();
                timelines.value = d.timelines || [];
            } catch(e) {}
        };

        const openTimelinePanel = (forceOpen = false) => {
            const nextOpen = forceOpen || !timelinePanelOpen.value;
            enterAdvancedPanel();
            fetchTimelines();
            timelinePanelOpen.value = nextOpen;
        };

        const openNewTimelineModal = () => {
            tlEditData.value = { id: null, label: '', color: '#5b9cf5', current_node_id: currentNode.value?.id || 0, charSet: new Set() };
            showTlEditModal.value = true;
        };

        const editTimeline = tl => {
            const ids = new Set((tl.char_ids||'').split(',').filter(x=>x.trim()).map(Number));
            tlEditData.value = { id: tl.id, label: tl.label, color: tl.color, current_node_id: tl.current_node_id || 0, charSet: ids };
            showTlEditModal.value = true;
        };

        const toggleTlChar = id => {
            const s = new Set(tlEditData.value.charSet);
            s.has(id) ? s.delete(id) : s.add(id);
            tlEditData.value.charSet = s;
        };

        const saveTlEdit = async () => {
            const d = tlEditData.value;
            if (!d.label.trim()) return;
            const char_ids = [...d.charSet].join(',');
            const body = { label: d.label, color: d.color, char_ids, current_node_id: d.current_node_id || 0 };
            try {
                if (d.id) {
                    // 编辑：PUT
                    await fetch(`${API_BASE_URL}/api/timelines/${d.id}`, {
                        method: 'PUT', headers: accountHeaders(true),
                        body: JSON.stringify({ label: d.label, color: d.color, char_ids, status: 'active' })
                    });
                    // 同步节点
                    if (d.current_node_id) {
                        await fetch(`${API_BASE_URL}/api/timelines/${d.id}/jump`, {
                            method: 'POST', headers: accountHeaders(true),
                            body: JSON.stringify({ node_id: d.current_node_id })
                        });
                    }
                } else {
                    // 新建：POST
                    await fetch(`${API_BASE_URL}/api/timelines`, {
                        method: 'POST', headers: accountHeaders(true),
                        body: JSON.stringify(body)
                    });
                }
                showTlEditModal.value = false;
                await fetchTimelines();
            } catch(e) {}
        };

        const deleteTimeline = async id => {
            if (!confirm('确认删除该时间线？记忆流将一并清除。')) return;
            await fetch(`${API_BASE_URL}/api/timelines/${id}`, { method: 'DELETE', headers: accountHeaders() });
            await fetchTimelines();
        };

        const bindCurrentSceneToTimeline = async tlId => {
            if (!currentNode.value) return;
            await fetch(`${API_BASE_URL}/api/timelines/${tlId}/jump`, {
                method: 'POST', headers: accountHeaders(true),
                body: JSON.stringify({ node_id: currentNode.value.id })
            });
            await fetchTimelines();
        };

        // ── 时间线记忆流 ──
        const openTlMemory = async tl => {
            tlMemoryTarget.value = tl;
            try {
                const r = await fetch(`${API_BASE_URL}/api/timelines/${tl.id}/memory`);
                tlMemoryContent.value = (await r.json()).content || '';
            } catch(e) { tlMemoryContent.value = ''; }
            showTlMemoryModal.value = true;
        };

        const saveTlMemory = async () => {
            if (!tlMemoryTarget.value) return;
            await fetch(`${API_BASE_URL}/api/timelines/${tlMemoryTarget.value.id}/memory`, {
                method: 'PUT', headers: accountHeaders(true),
                body: JSON.stringify({ content: tlMemoryContent.value })
            });
            showTlMemoryModal.value = false;
        };

        // ── 独立推演 ──
        const openTlDynamicModal = tl => {
            tlDynamicTarget.value = tl;
            tlDynamicAction.value = '';
            showTlDynamicModal.value = true;
        };

        const runTlDynamic = async () => {
            const tl = tlDynamicTarget.value;
            if (!tl || !tl.current_node_id || !tlDynamicAction.value.trim()) return;
            const node = storyNodes.value.find(n => n.id === tl.current_node_id);
            if (!node) return;
            isTlDynamicRunning.value = true;
            try {
                const r = await fetch(`${API_BASE_URL}/api/timelines/${tl.id}/dynamic-options`, {
                    method: 'POST', headers: accountHeaders(true),
                    body: JSON.stringify({
                        timeline_id: tl.id,
                        current_node_id: tl.current_node_id,
                        scene_name: node.name,
                        content: node.content || '',
                        player_action: tlDynamicAction.value,
                        action_type: actionType.value || 'mixed'
                    })
                });
                const d = await r.json();
                if (d.status === 'success') {
                    showTlDynamicModal.value = false;
                    await fetchGameState();
                    await fetchTimelines();
                    if (d.spawned_npc) showNpcToast(d.spawned_npc);
                    if (d.stat_changes && d.stat_changes.length > 0) {
                        statChangesLog.value = d.stat_changes;
                        showStatChanges.value = true;
                        if (statChangesTimer) clearTimeout(statChangesTimer);
                        statChangesTimer = setTimeout(() => { showStatChanges.value = false; }, 10000);
                    }
                }
            } catch(e) {}
            finally { isTlDynamicRunning.value = false; }
        };

        // ── 汇合方法 ──
        const openMergeModal = tl => {
            mergeSource.value = tl;
            mergeTargetId.value = 0;
            showMergeModal.value = true;
        };

        const runMerge = async () => {
            if (!mergeSource.value || !mergeTargetId.value) return;
            isMerging.value = true;
            try {
                const r = await fetch(`${API_BASE_URL}/api/timelines/merge`, {
                    method: 'POST', headers: accountHeaders(true),
                    body: JSON.stringify({ source_id: mergeSource.value.id, target_id: mergeTargetId.value })
                });
                const d = await r.json();
                if (d.status === 'success') {
                    showMergeModal.value = false;
                    await fetchTimelines();
                    await fetchGameState();
                    await fetchWorldEntities();
                }
            } catch(e) {}
            finally { isMerging.value = false; }
        };

        // 时间线地图位置：打开模态框
        const openTlRoomModal = (tl) => {
            tlRoomTarget.value   = tl;
            tlRoomSelected.value = tl.current_room_id || null;
            showTlRoomModal.value = true;
        };

        // 时间线地图位置：保存
        const saveTlRoom = async () => {
            if (!tlRoomTarget.value) return;
            const rid = tlRoomSelected.value;
            await mapSetTimelineRoom(tlRoomTarget.value.id, rid);
            // 同步更新本地 timelines 列表
            const tl = timelines.value.find(t => t.id === tlRoomTarget.value.id);
            if (tl) tl.current_room_id = rid;
            showTlRoomModal.value = false;
        };

        // ══════════════════════════════════════════════════════
        // 【世界实体注册表】：响应式状态与方法
        // ══════════════════════════════════════════════════════
        const showWorldEntitiesModal = ref(false);
        const worldEntities = ref([]);
        const currentEntity = ref(null);
        const entityFilter = ref('all');

        const filteredEntities = computed(() => {
            if (entityFilter.value === 'all') return worldEntities.value;
            return worldEntities.value.filter(e => e.entity_type === entityFilter.value);
        });

        // ══════════════════════════════════════════════════════
        // 【分屏模式】：计算属性与行内推演状态
        // ══════════════════════════════════════════════════════
        const activeTimelines = computed(() =>
            timelines.value.filter(t => t.status === 'active')
        );

        // 每条时间线独立的输入框内容：{ [tl.id]: string }
        const tlActionInputs = ref({});
        // 每条时间线独立的推演模式：{ [tl.id]: 'mixed'|'dialogue'|'action' }，默认 'mixed'
        const tlActionTypes = ref({});
        const getTlActionType = (tlId) => tlActionTypes.value[tlId] || 'mixed';
        // 每条时间线独立的最近推演 thought context：{ [tl.id]: {thoughtProcess} }
        const tlLastContexts = ref({});
        // 正在推演中的时间线 id 集合
        const tlRunningIds = ref(new Set());

        // 分屏内行内推演（直接复用后端 timeline dynamic-options）
        const runTlDynamicInline = async (tl) => {
            const action = (tlActionInputs.value[tl.id] || '').trim();
            if (!action || !tl.current_node_id || tlRunningIds.value.has(tl.id)) return;
            const node = storyNodes.value.find(n => n.id === tl.current_node_id);
            if (!node) return;

            tlRunningIds.value = new Set([...tlRunningIds.value, tl.id]);
            try {
                const r = await fetch(`${API_BASE_URL}/api/timelines/${tl.id}/dynamic-options`, {
                    method: 'POST', headers: accountHeaders(true),
                    body: JSON.stringify({
                        timeline_id: tl.id,
                        current_node_id: tl.current_node_id,
                        scene_name: node.name,
                        content: node.content || '',
                        player_action: action,
                        action_type: getTlActionType(tl.id)
                    })
                });
                const d = await r.json();
                if (d.status === 'success') {
                    tlActionInputs.value = { ...tlActionInputs.value, [tl.id]: '' };
                    if (d.thought_process) tlLastContexts.value = { ...tlLastContexts.value, [tl.id]: { thoughtProcess: d.thought_process } };
                    await fetchGameState();
                    await fetchTimelines();
                    if (d.spawned_npc) showNpcToast(d.spawned_npc);
                    if (d.stat_changes?.length) {
                        statChangesLog.value = d.stat_changes;
                        showStatChanges.value = true;
                        if (statChangesTimer) clearTimeout(statChangesTimer);
                        statChangesTimer = setTimeout(() => { showStatChanges.value = false; }, 10000);
                    }
                }
            } catch(e) {}
            finally {
                const next = new Set(tlRunningIds.value);
                next.delete(tl.id);
                tlRunningIds.value = next;
            }
        };

        // 分屏内点击分支选项：更新时间线节点并刷新
        const tlJumpAndBind = async (tlId, nodeId, optionText = '') => {
            const targetNode = storyNodes.value.find(n => n.id === nodeId);
            const tl = timelines.value.find(t => t.id === tlId);

            if (targetNode) {
                try {
                    // 执行已生成的确定性副作用；时间线预设行动也不自动调用聊天模型扩写。
                    await applyBranchEffects(nodeId);

                    // 只有通过选项正常推进时才同步时间线地图位置
                    if (optionText) {
                        try {
                            const roomRes = await fetch(`${API_BASE_URL}/api/map/room-by-node/${nodeId}`);
                            const roomData = await roomRes.json();
                            if (roomData.room_id) {
                                await fetch(`${API_BASE_URL}/api/map/move?room_id=${roomData.room_id}&timeline_id=${tlId}`, { method: 'POST', headers: accountHeaders() });
                                await fetchMapData();
                            }
                        } catch(_) {}
                    }
                } catch(e) { console.error('tl branch expand/apply failed:', e); }
            }
            await fetch(`${API_BASE_URL}/api/timelines/${tlId}/jump`, {
                method: 'POST', headers: accountHeaders(true),
                body: JSON.stringify({ node_id: nodeId })
            });
            // 同步记录到全局记忆流
            const node = storyNodes.value.find(n => n.id === nodeId);
            if (node) {
                fetch(`${API_BASE_URL}/api/game/log-scene-visit`, {
                    method: 'POST', headers: accountHeaders(true),
                    body: JSON.stringify({ node_id: nodeId, node_name: node.name, option_text: optionText })
                }).catch(() => {});
            }
            await fetchGameState();
            await fetchTimelines();
        };

        const fetchWorldEntities = async () => {
            try {
                const r = await fetch(`${API_BASE_URL}/api/world-entities`);
                worldEntities.value = (await r.json()).entities || [];
            } catch(e) {}
        };

        const openWorldEntitiesPanel = () => {
            enterAdvancedPanel();
            fetchWorldEntities();
            showWorldEntitiesModal.value = true;
        };

        const openNewEntityForm = () => {
            currentEntity.value = {
                id: null, entity_type: 'npc', name: '',
                location: '', status: 'active', last_seen_by: '', state_desc: ''
            };
        };

        const saveEntity = async () => {
            if (!currentEntity.value || !currentEntity.value.name.trim()) return;
            const e = currentEntity.value;
            try {
                await fetch(`${API_BASE_URL}/api/world-entities`, {
                    method: 'POST',
                    headers: accountHeaders(true),
                    body: JSON.stringify({
                        entity_type: e.entity_type,
                        name: e.name.trim(),
                        location: e.location || '',
                        status: e.status,
                        last_seen_by: e.last_seen_by || '',
                        state_desc: e.state_desc || ''
                    })
                });
                currentEntity.value = null;
                await fetchWorldEntities();
            } catch(err) {}
        };

        const deleteEntity = async id => {
            if (!confirm('确认从世界状态中移除该实体记录？')) return;
            await fetch(`${API_BASE_URL}/api/world-entities/${id}`, { method: 'DELETE', headers: accountHeaders() });
            currentEntity.value = null;
            await fetchWorldEntities();
        };

        // ══════════════════════════════════════════════════════
        // 【RAG 知识库】：响应式状态与方法
        // ══════════════════════════════════════════════════════
        const showRagModal     = ref(false);
        const ragTab           = ref('import');
        const ragDocuments     = ref([]);
        const ragSelectedDoc   = ref(null);
        const ragForm          = ref({ title: '', source: '', text: '', hidden: false });
        const ragIngesting     = ref(false);
        const ragIngestResult  = ref(null);
        const ragSearchQuery   = ref('');
        const ragSearching     = ref(false);
        const ragSearchResults = ref([]);
        // 文件上传专属状态
        const ragUploadFile    = ref(null);   // File 对象
        const ragDragOver      = ref(false);
        const ragChunkSize     = ref(600);
        const ragChunkOverlap  = ref(80);
        const ragTopK          = ref(6);

        const fetchRagDocuments = async (options = {}) => {
            try {
                const includeHidden = !!options.includeHidden;
                const url = `${API_BASE_URL}/api/rag/documents${includeHidden ? '?include_hidden=1' : ''}`;
                const r = await fetch(url, includeHidden ? { headers: accountHeaders() } : undefined);
                ragDocuments.value = (await r.json()).documents || [];
            } catch(e) {}
        };

        const openRagModal = () => {
            enterAdvancedPanel();
            fetchRagDocuments({ includeHidden: true });
            ragTab.value = 'import';
            ragIngestResult.value = null;
            ragUploadFile.value   = null;
            showRagModal.value    = true;
        };

        // 文件选择（<input type="file"> change）
        const ragHandleFileSelect = (e) => {
            const f = e.target.files[0];
            if (!f) return;
            ragUploadFile.value = f;
            // 自动填入标题（如果为空）
            if (!ragForm.value.title.trim())
                ragForm.value.title = f.name.replace(/\.[^/.]+$/, '');
        };

        // 拖拽放入
        const ragHandleDrop = (e) => {
            ragDragOver.value = false;
            const f = e.dataTransfer.files[0];
            if (!f) return;
            const ext = f.name.split('.').pop().toLowerCase();
            if (!['txt','md','markdown','pdf','docx','doc'].includes(ext)) {
                ragIngestResult.value = { ok: false, msg: '仅支持 .txt、.md、.pdf、.docx、.doc 文件' };
                return;
            }
            ragUploadFile.value = f;
            if (!ragForm.value.title.trim())
                ragForm.value.title = f.name.replace(/\.[^/.]+$/, '');
        };

        // 统一入口：文件上传 或 文本粘贴
        const ragImport = async () => {
            ragIngesting.value    = true;
            ragIngestResult.value = null;
            try {
                let d;
                if (ragUploadFile.value) {
                    // ── 文件上传路径 ──────────────────────────────
                    const fd = new FormData();
                    fd.append('file',          ragUploadFile.value);
                    fd.append('title',         ragForm.value.title.trim()  || ragUploadFile.value.name);
                    fd.append('source',        ragForm.value.source.trim() || ragUploadFile.value.name);
                    fd.append('chunk_size',    ragChunkSize.value);
                    fd.append('chunk_overlap', ragChunkOverlap.value);
                    fd.append('hidden',        ragForm.value.hidden ? 1 : 0);
                    const r = await fetch(`${API_BASE_URL}/api/rag/upload`, {
                        method: 'POST',
                        headers: accountHeaders(),
                        body: fd          // 不设 Content-Type，让浏览器自动设置 multipart/form-data
                    });
                    d = await r.json();
                    if (d.status === 'success') {
                        ragIngestResult.value = {
                            ok:  true,
                            msg: `✓ 上传成功：${d.filename}，共 ${d.char_count} 字 → ${d.chunk_count} 个切片，${d.embedded} 个已向量化（${d.elapsed_sec}s）`
                        };
                    }
                } else {
                    // ── 文本粘贴路径 ──────────────────────────────
                    if (!ragForm.value.title.trim() || !ragForm.value.text.trim()) {
                        ragIngestResult.value = { ok: false, msg: '请填写标题和正文，或上传文件' };
                        return;
                    }
                    const r = await fetch(`${API_BASE_URL}/api/rag/ingest`, {
                        method: 'POST',
                        headers: accountHeaders(true),
                        body: JSON.stringify({
                            title:         ragForm.value.title.trim(),
                            source:        ragForm.value.source.trim(),
                            text:          ragForm.value.text.trim(),
                            chunk_size:    ragChunkSize.value,
                            chunk_overlap: ragChunkOverlap.value,
                            hidden:        ragForm.value.hidden ? 1 : 0,
                        })
                    });
                    d = await r.json();
                    if (d.status === 'success') {
                        ragIngestResult.value = {
                            ok:  true,
                            msg: `✓ 导入成功：${d.chunk_count} 个切片，${d.embedded} 个已向量化（${d.elapsed_sec}s）`
                        };
                    }
                }

                if (d && d.status === 'success') {
                    ragForm.value       = { title: '', source: '', text: '', hidden: false };
                    ragUploadFile.value = null;
                    await fetchRagDocuments({ includeHidden: true });
                    ragTab.value        = 'view';
                    ragSelectedDoc.value = ragDocuments.value[0] || null;
                } else if (d) {
                    ragIngestResult.value = { ok: false, msg: d.message || '操作失败' };
                }
            } catch(e) {
                ragIngestResult.value = { ok: false, msg: '网络错误，请检查引擎是否运行' };
            } finally {
                ragIngesting.value = false;
            }
        };

        const ragDeleteDoc = async id => {
            if (!confirm('确认删除此文档及其所有切片？此操作不可恢复。')) return;
            await fetch(`${API_BASE_URL}/api/rag/documents/${id}`, { method: 'DELETE', headers: accountHeaders() });
            ragSelectedDoc.value = null;
            await fetchRagDocuments({ includeHidden: true });
        };

        const ragToggleHidden = async doc => {
            const newHidden = doc.hidden ? 0 : 1;
            await fetch(`${API_BASE_URL}/api/rag/documents/${doc.id}/hidden?hidden=${newHidden}`, { method: 'PATCH', headers: accountHeaders() });
            await fetchRagDocuments({ includeHidden: true });
            ragSelectedDoc.value = ragDocuments.value.find(d => d.id === doc.id) || null;
        };

        const ragSearch = async () => {
            if (!ragSearchQuery.value.trim()) return;
            ragSearching.value    = true;
            ragSearchResults.value = [];
            try {
                const r = await fetch(`${API_BASE_URL}/api/rag/search`, {
                    method: 'POST',
                    headers: accountHeaders(true),
                    body: JSON.stringify({ scene_name: ragSearchQuery.value, content: '', top_k: ragTopK.value })
                });
                const d = await r.json();
                ragSearchResults.value = d.results || [];
            } catch(e) {}
            finally { ragSearching.value = false; }
        };
        // 将后端树结构拍平为带 joinOp 的条件列表（左结合展开）
        const _flattenTree = (node, depth = 0) => {
            if (!node) return [];
            if (node.op === 'not') {
                const inner = node.children && node.children[0];
                if (!inner) return [];
                if (inner.op) {
                    const sub = _flattenTree(inner, depth);
                    if (sub.length) sub[0].negate = !sub[0].negate;
                    return sub;
                }
                return [{ type: inner.type||'', value: inner.value||'', negate: true }];
            }
            if (node.op === 'and' || node.op === 'or') {
                const result = [];
                let hasGroupedChild = false;
                (node.children || []).forEach((child, i) => {
                    if (child.op === 'and' || child.op === 'or') hasGroupedChild = true;
                    const sub = _flattenTree(child, depth + 1);
                    if (i > 0 && sub.length > 0) sub[0].joinOp = node.op;
                    result.push(...sub);
                });
                if (hasGroupedChild && result.length) result[0]._groupingLost = true;
                return result;
            }
            // 叶节点
            return [{ type: node.type||'', value: node.value||'', negate: false }];
        };
        // 将带 joinOp 的条件列表构建为左结合树（发给后端）
        const _buildTree = (conditions) => {
            const valid = conditions.filter(c => c.type && c.value);
            if (!valid.length) return { op: 'and', children: [] };
            const toNode = c => {
                const leaf = { type: c.type, value: c.value };
                return c.negate ? { op: 'not', children: [leaf] } : leaf;
            };
            let tree = toNode(valid[0]);
            for (let i = 1; i < valid.length; i++)
                tree = { op: valid[i].joinOp || 'and', children: [tree, toNode(valid[i])] };
            if (!tree.op) tree = { op: 'and', children: [tree] };
            return tree;
        };
        const fetchTriggers = async () => {
            try {
                const r = await fetch(`${API_BASE_URL}/api/game/triggers`);
                const data = await r.json();
                triggers.value = (data.triggers || []).map(t => {
                    if (t.conditions && !Array.isArray(t.conditions) && Array.isArray(t.conditions.children)) {
                        t.conditions = _flattenTree(t.conditions);
                        t._hasGroupedConditions = t.conditions.some(c => c._groupingLost);
                    } else {
                        if (!Array.isArray(t.conditions)) t.conditions = [];
                        t.conditions = t.conditions.map(c => ({ ...c, negate: c.negate||false }));
                        t._hasGroupedConditions = false;
                    }
                    return t;
                });
            } catch(e) {}
        };
        const openTriggerModal = () => { enterAdvancedPanel(); fetchTriggers(); showTriggerModal.value=true; };
        const newTrigger = () => { currentTrigger.value={label:'',target_node_id:'',mode:'soft',conditions:[{type:'scene',value:'',negate:false}],fired:0,fire_count:0,cooldown:0,max_fire_count:0,actions:[],prerequisite_trigger_ids:[],exclude_trigger_ids:[]}; };
        const selectTrigger = t => { const copy=JSON.parse(JSON.stringify(t)); if(!Array.isArray(copy.conditions)||!copy.conditions.length){if(copy.cond_type)copy.conditions=[{type:copy.cond_type,value:copy.cond_value||'',negate:false}];else copy.conditions=[];} if(!Array.isArray(copy.actions))copy.actions=[]; currentTrigger.value=copy; };
        const saveTrigger = async () => {
            const t = currentTrigger.value;
            if ((!t.target_node_id && t.mode !== 'passive' && !t.actions?.some(a => a.type === 'gen_node')) || !t.conditions || !t.conditions.some(c => c.value)) return;
            const isNew = !t.id;
            const url = isNew ? `${API_BASE_URL}/api/game/trigger` : `${API_BASE_URL}/api/game/trigger/${t.id}`;
            const payload = { label: t.label||'触发器', target_node_id: t.target_node_id ? parseInt(t.target_node_id) : 0, mode: t.mode, conditions: _buildTree(t.conditions), cooldown: t.cooldown||0, max_fire_count: t.max_fire_count||0, actions: t.actions||[], prerequisite_trigger_ids: t.prerequisite_trigger_ids||[], exclude_trigger_ids: t.exclude_trigger_ids||[] };
            try { const r=await fetch(url,{method:isNew?'POST':'PUT',headers:accountHeaders(true),body:JSON.stringify(payload)});
                if(r.ok){await fetchTriggers();currentTrigger.value=null;} } catch(e){}
        };
        const addTriggerAction = () => {
            if (!currentTrigger.value.actions) currentTrigger.value.actions = [];
            currentTrigger.value.actions.push({type:'set_flag', key:'', value:'1'});
        };
        const resetActionFields = (act) => {
            const defaults = {
                set_flag:      {key:'', value:'1'},
                mod_stat:      {target:'PC', attr:'hp', delta:0},
                add_memory:    {text:''},
                fire_trigger:  {id:''},
                inject_text:   {text:''},
                inject_option: {text:'', next_node_id:0},
                gen_node:      {prompt:'', mode:'soft'},
                mod_inventory: {target:'PC', op:'add', item:''},
                mod_npc:       {name:'', field:'emotion_trust', delta:0},
                entity_script: {name:'', action:'appear'},
                spawn_npc:     {name:'', hp:100, san:80, inventory:'', desc:''},
                mod_worldview: {op:'append', text:''},
                add_lore:      {keywords:'', content:''},
                reveal_rag:    {doc_id: 0},
            };
            const t = act.type; Object.keys(act).forEach(k=>{ if(k!=='type') delete act[k]; }); Object.assign(act, defaults[t]||{});
        };
        const deleteTrigger = async id => { if(!confirm('彻底删除此触发器？'))return; await fetch(`${API_BASE_URL}/api/game/trigger/${id}`,{method:'DELETE',headers:accountHeaders()}); await fetchTriggers(); currentTrigger.value=null; };
        const resetTriggerFired = async id => { await fetch(`${API_BASE_URL}/api/game/trigger/${id}/reset`,{method:'POST',headers:accountHeaders()}); await fetchTriggers(); if(currentTrigger.value&&currentTrigger.value.id===id) currentTrigger.value={...currentTrigger.value,fired:0,fire_count:0}; };
        const resetAllTriggers = async () => { if(!confirm('将所有触发器重置为未触发状态？'))return; await fetch(`${API_BASE_URL}/api/game/triggers/reset-all`,{method:'POST',headers:accountHeaders()}); await fetchTriggers(); if(currentTrigger.value) currentTrigger.value={...currentTrigger.value,fired:0,fire_count:0}; };

        // ── Campaigns ──
        const fetchCampaigns = async () => {
            try {
                const r = await fetch(`${API_BASE_URL}/api/campaigns`, { headers: accountHeaders() });
                const d = await r.json();
                if (!r.ok || d.status !== 'success') throw new Error(d.message || d.detail || '获取剧本列表失败');
                let raw = Array.isArray(d.files) ? d.files : [];
                campaignFiles.value = raw;
                const selected = normalizeCampaignPath(selectedCampaign.value);
                const stillSelected = raw.find(f => campaignSelectionMatches(f, selected));
                const def = raw.find(f => f.name === 'campaign_settings.json' || f.name === 'campaign_settings');
                selectedCampaign.value = (stillSelected || def || raw[0] || {}).path || '';
                return raw;
            } catch(e) {
                return null;
            }
        };
        const fetchSaves = async () => {
            try {
                const r = await fetch(`${API_BASE_URL}/api/game/saves`, { headers: accountHeaders() });
                const d = await r.json();
                if (!r.ok || d.status !== 'success') throw new Error(d.message || d.detail || '获取存档列表失败');
                saveFiles.value = Array.isArray(d.files)
                    ? d.files.filter(save => normalizeCampaignPath(save?.path || '').startsWith('saves/'))
                    : [];
                return saveFiles.value;
            } catch(e) {
                saveFiles.value = [];
                return null;
            }
        };
        const deleteSelectedCampaign = async () => {
            const selected = selectedCampaignInfo.value;
            if (!requireMultiplayerAuth()) return;
            if (!selected?.deletable || !selected.name || isDeletingCampaign.value) return;
            const label = selected.name || selected.path || '当前剧本';
            if (!confirm(`删除剧本「${label}」？\n\n会删除该 campaigns 文件夹内的剧本、知识库、地图和图片资源。`)) return;
            isDeletingCampaign.value = true;
            campaignLoadSummary.value = '';
            try {
                const r = await fetch(`${API_BASE_URL}/api/campaigns/${encodeURIComponent(selected.name)}`, {
                    method: 'DELETE',
                    headers: accountHeaders(),
                });
                const d = await r.json().catch(() => ({}));
                if (!r.ok || d.status !== 'success') throw new Error(d.message || d.detail || '删除失败');
                if (currentSaveFolder.value === selected.name) currentSaveFolder.value = '';
                if (selectedCampaign.value === selected.path) selectedCampaign.value = '';
                await fetchCampaigns();
                campaignLoadSummary.value = d.message || `已删除剧本：${label}`;
            } catch(e) {
                campaignLoadSummary.value = e?.message || '删除失败';
            } finally {
                isDeletingCampaign.value = false;
            }
        };
        const exportCampaignPackage = async (campaign = selectedCampaignInfo.value) => {
            if (!requireMultiplayerAuth()) return;
            if (!campaign?.package_export_url || campaignPackageExportBusy.value) return;
            campaignPackageExportBusy.value = true;
            campaignLoadSummary.value = '';
            try {
                const r = await fetch(`${API_BASE_URL}${campaign.package_export_url}`, { headers: accountHeaders() });
                if (!r.ok) {
                    const d = await r.json().catch(() => ({}));
                    throw new Error(d.message || d.detail || '导出迁移包失败');
                }
                const blob = await r.blob();
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = `${campaign.name || 'zric-campaign'}_campaign_package.zip`;
                document.body.appendChild(a);
                a.click();
                a.remove();
                URL.revokeObjectURL(url);
                campaignLoadSummary.value = `已导出迁移包：${campaign.name || campaign.path}`;
            } catch(e) {
                campaignLoadSummary.value = e?.message || '导出迁移包失败';
            } finally {
                campaignPackageExportBusy.value = false;
            }
        };
        const importCampaignPackageFile = async (file) => {
            if (!file || campaignPackageImportBusy.value) return;
            if (!requireMultiplayerAuth()) return;
            campaignPackageImportBusy.value = true;
            campaignLoadSummary.value = '';
            try {
                const fd = new FormData();
                fd.append('package_file', file);
                const r = await fetch(`${API_BASE_URL}/api/campaigns/package/import`, {
                    method: 'POST',
                    headers: accountHeaders(),
                    body: fd,
                });
                const d = await r.json().catch(() => ({}));
                if (!r.ok || d.status !== 'success') throw new Error(d.message || d.detail || '导入迁移包失败');
                await fetchCampaigns();
                selectedCampaign.value = d.campaign_path || d.path || selectedCampaign.value;
                campaignLoadSummary.value = d.message || `已导入迁移包：${d.name || file.name}`;
                showCampaignImportOutcome({
                    ok: true,
                    title: '迁移包导入成功',
                    message: `已导入「${d.name || file.name}」，可在启动页剧本列表中选择。`,
                    path: d.campaign_path || d.path || '',
                    stats: {
                        scenes: d.nodes_count || 0,
                        characters: d.characters_count || 0,
                        rooms: d.map_rooms_count || 0,
                        knowledge: d.knowledge_count || 0,
                        assets: d.assets_count || 0,
                    },
                });
            } catch(e) {
                campaignLoadSummary.value = e?.message || '导入迁移包失败';
                showCampaignImportOutcome({
                    ok: false,
                    title: '迁移包导入失败',
                    message: campaignLoadSummary.value,
                });
            } finally {
                campaignPackageImportBusy.value = false;
            }
        };
        const campaignPackagePickImport = async (e) => {
            const file = e.target.files?.[0];
            e.target.value = '';
            if (file) await importCampaignPackageFile(file);
        };
        const findImportedCampaign = (files, result) => {
            const expectedPath = String(result?.campaign_path || '').replace(/\\/g, '/');
            const expectedName = String(result?.name || expectedPath.split('/').pop() || '');
            return (files || []).find(f => {
                const path = String(f.path || '').replace(/\\/g, '/');
                return (expectedPath && path === expectedPath) || (expectedName && f.name === expectedName);
            });
        };
        const showCampaignImportVerifiedNotice = (payload) => {
            campaignImportVerifiedNotice.value = payload;
            if (campaignImportNoticeTimer) clearTimeout(campaignImportNoticeTimer);
            campaignImportNoticeTimer = setTimeout(() => {
                campaignImportVerifiedNotice.value = null;
                campaignImportNoticeTimer = null;
            }, 12000);
        };
        const showCampaignImportOutcome = (payload) => {
            campaignImportOutcome.value = {
                ok: !!payload?.ok,
                title: payload?.title || (payload?.ok ? '剧本解析成功' : '剧本解析失败'),
                message: payload?.message || '',
                path: payload?.path || '',
                stats: payload?.stats || null,
                warnings: Array.isArray(payload?.warnings) ? payload.warnings.filter(Boolean).slice(-10) : [],
            };
        };
        const verifyCampaignImportVisible = async (result) => {
            campaignImportProgress.value = 100;
            campaignImportProgressStep.value = 'verify';
            campaignImportProgressMessage.value = '正在确认剧本已加入启动页列表';
            const files = await fetchCampaigns();
            if (!Array.isArray(files)) {
                throw new Error('剧本文件已生成，但刷新剧本列表失败，无法确认是否已加入启动页');
            }
            const imported = findImportedCampaign(files, result);
            if (!imported) {
                const target = result?.campaign_path || result?.name || '新剧本';
                throw new Error(`剧本文件已生成，但刷新剧本列表后未找到「${target}」。请检查账号可见性、campaigns 目录权限或服务缓存。`);
            }
            selectedCampaign.value = imported.path || result?.campaign_path || selectedCampaign.value;
            return imported;
        };
        const fetchCampaignImportFormats = async () => {
            try {
                const r = await fetch(`${API_BASE_URL}/api/campaigns/import/formats`);
                const d = await r.json();
                if (d.status === 'success') campaignImportFormats.value = d.formats || [];
            } catch(e) {}
        };
        const openCampaignImportModal = () => {
            if (!requireMultiplayerAuth()) return;
            campaignImportName.value = '';
            campaignImportMainFile.value = null;
            campaignImportAssets.value = [];
            campaignImportOcrEnabled.value = true;
            campaignImportResult.value = null;
            campaignImportWarnings.value = [];
            campaignImportErrorText.value = '';
            campaignImportJobId.value = '';
            campaignImportProgress.value = 0;
            campaignImportProgressStep.value = '等待';
            campaignImportProgressMessage.value = '等待上传剧本文档';
            campaignImportVerifiedNotice.value = null;
            campaignImportOutcome.value = null;
            showCampaignImportModal.value = true;
            fetchCampaignImportFormats();
        };
        const campaignImportPickMain = (e) => {
            const f = e.target.files?.[0];
            if (!f) return;
            campaignImportMainFile.value = f;
            if (!campaignImportName.value.trim()) campaignImportName.value = f.name.replace(/\.[^/.]+$/, '');
        };
        const campaignImportPickAssets = (e) => {
            campaignImportAssets.value = Array.from(e.target.files || []);
        };
        const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));
        const syncCampaignImportJob = (job) => {
            if (!job) return;
            campaignImportProgress.value = Number.isFinite(job.progress) ? Math.max(0, Math.min(100, job.progress)) : campaignImportProgress.value;
            campaignImportProgressStep.value = job.step || campaignImportProgressStep.value;
            campaignImportProgressMessage.value = job.message || campaignImportProgressMessage.value;
            campaignImportWarnings.value = Array.isArray(job.warnings) ? job.warnings.filter(Boolean) : [];
            if (job.error) campaignImportErrorText.value = formatCampaignImportError(job.error);
        };
        const pollCampaignImportJob = async (jobId) => {
            const startedAt = Date.now();
            let lastProgressKey = '';
            let lastProgressAt = startedAt;
            while (campaignImportBusy.value && jobId) {
                if (Date.now() - startedAt > CAMPAIGN_IMPORT_POLL_TIMEOUT_MS) {
                    const msg = '剧本解析等待超时；请检查服务器日志、MinerU token/网络配置，或降低导入文件复杂度后重试';
                    campaignImportErrorText.value = msg;
                    throw new Error(msg);
                }
                const r = await fetch(`${API_BASE_URL}/api/campaigns/import/${encodeURIComponent(jobId)}`, { headers: accountHeaders() });
                const d = await r.json().catch(() => ({}));
                if (!r.ok || d.status !== 'success') {
                    const msg = formatCampaignImportError(d.detail || d.message, '读取解析进度失败');
                    campaignImportErrorText.value = msg;
                    throw new Error(msg);
                }
                const job = d.job || {};
                syncCampaignImportJob(job);
                const progressKey = `${job.status || ''}|${job.step || ''}|${job.progress || 0}|${job.message || ''}`;
                if (progressKey !== lastProgressKey) {
                    lastProgressKey = progressKey;
                    lastProgressAt = Date.now();
                } else if (Date.now() - lastProgressAt > CAMPAIGN_IMPORT_NO_PROGRESS_TIMEOUT_MS) {
                    const msg = `剧本解析在「${job.message || job.step || '当前步骤'}」停留过久；请检查 MinerU CLI/token、网络连接，或降低文件页数后重试`;
                    campaignImportErrorText.value = msg;
                    throw new Error(msg);
                }
                if (job.status === 'success') return job;
                if (job.status === 'error') {
                    const msg = formatCampaignImportError(job.error, '剧本解析失败');
                    campaignImportErrorText.value = msg;
                    throw new Error(msg);
                }
                await sleep(700);
            }
            campaignImportErrorText.value = '剧本解析已中断';
            throw new Error('剧本解析已中断');
        };
        const importCampaign = async () => {
            if (!campaignImportMainFile.value || campaignImportBusy.value) return;
            if (!requireMultiplayerAuth()) return;
            campaignImportBusy.value = true;
            campaignImportResult.value = null;
            campaignImportWarnings.value = [];
            campaignImportErrorText.value = '';
            campaignImportJobId.value = '';
            campaignImportProgress.value = 2;
            campaignImportProgressStep.value = 'upload';
            campaignImportProgressMessage.value = '正在上传剧本文档';
            try {
                const fd = new FormData();
                fd.append('name', campaignImportName.value.trim());
                fd.append('ocr_enabled', campaignImportOcrEnabled.value ? 'true' : 'false');
                fd.append('main_file', campaignImportMainFile.value);
                for (const asset of campaignImportAssets.value) fd.append('assets', asset);
                const r = await fetch(`${API_BASE_URL}/api/campaigns/import`, { method: 'POST', headers: accountHeaders(), body: fd });
                const d = await r.json().catch(() => ({}));
                if (!r.ok) {
                    const msg = formatCampaignImportError(d.detail || d.message, '导入接口返回错误');
                    campaignImportErrorText.value = msg;
                    throw new Error(msg);
                }
                if (d.status === 'accepted' && d.job_id) {
                    campaignImportJobId.value = d.job_id;
                    syncCampaignImportJob(d.job);
                    const job = await pollCampaignImportJob(d.job_id);
                    const result = job.result || {};
                    const imported = await verifyCampaignImportVisible(result);
                    const importedName = imported.name || result.name || '新剧本';
                    const importedPath = imported.path || result.campaign_path || '';
                    const backendStatus = result.import_verified ? '后端文件校验通过' : '后端已返回完成状态';
                    campaignImportResult.value = {
                        ok: true,
                        title: '导入已确认',
                        message: `${backendStatus}，并已确认「${importedName}」出现在启动页剧本列表，已自动选中。\n路径：${importedPath}\n内容：${result.nodes_count || 0} 个场景，${result.characters_count || 0} 个角色，${result.lore_count || 0} 条百科，${result.map_rooms_count || 0} 个地图房间，${result.knowledge_count || 0} 个知识库文档，${result.assets_count || 0} 个图片资源。`,
                        warnings: job.warnings || []
                    };
                    showCampaignImportOutcome({
                        ok: true,
                        title: '剧本导入成功',
                        message: `已确认「${importedName}」写入本地并出现在启动页列表。`,
                        path: importedPath,
                        stats: {
                            scenes: result.nodes_count || 0,
                            characters: result.characters_count || 0,
                            lore: result.lore_count || 0,
                            entities: result.entities_count || 0,
                            rooms: result.map_rooms_count || 0,
                            knowledge: result.knowledge_count || 0,
                            assets: result.assets_count || 0,
                        },
                        warnings: job.warnings || [],
                    });
                    showCampaignImportVerifiedNotice({
                        title: '导入成功，已确认可选择',
                        message: `「${importedName}」已写入并出现在剧本列表。`,
                        path: importedPath,
                    });
                } else {
                    const msg = formatCampaignImportError(d.detail || d.message, '导入接口返回错误');
                    campaignImportErrorText.value = msg;
                    campaignImportResult.value = { ok: false, title: '解析失败', message: msg, warnings: campaignImportWarnings.value || [] };
                    showCampaignImportOutcome({
                        ok: false,
                        title: '剧本导入失败',
                        message: msg,
                        warnings: campaignImportWarnings.value || [],
                    });
                }
            } catch(e) {
                const msg = formatCampaignImportError(e?.message, '网络错误或服务器不可用');
                campaignImportErrorText.value = msg;
                campaignImportResult.value = {
                    ok: false,
                    title: '解析失败',
                    message: msg,
                    warnings: campaignImportWarnings.value || []
                };
                showCampaignImportOutcome({
                    ok: false,
                    title: '剧本导入失败',
                    message: msg,
                    warnings: campaignImportWarnings.value || [],
                });
            } finally {
                campaignImportBusy.value = false;
            }
        };
        const reparseSelectedCampaign = async () => {
            if (campaignImportBusy.value) return;
            if (!requireMultiplayerAuth()) return;
            const selected = selectedCampaignInfo.value;
            if (!selectedCampaign.value || selected?.type !== 'folder') {
                showCampaignImportOutcome({
                    ok: false,
                    title: '无法重新识别',
                    message: '请选择一个 campaigns/<剧本名> 文件夹剧本后再重新识别。',
                });
                return;
            }
            if (!confirm('重新识别会覆盖该剧本的结构化结果（campaign.json、map.json 和导入器生成的知识库分段），会保留原始文本与图片。继续？')) {
                return;
            }
            campaignImportBusy.value = true;
            campaignImportResult.value = null;
            campaignImportWarnings.value = [];
            campaignImportErrorText.value = '';
            campaignImportJobId.value = '';
            campaignImportOutcome.value = null;
            campaignImportProgress.value = 4;
            campaignImportProgressStep.value = 'reparse';
            campaignImportProgressMessage.value = '正在提交重新识别任务';
            try {
                const r = await fetch(`${API_BASE_URL}/api/campaigns/reparse`, {
                    method: 'POST',
                    headers: accountHeaders(true),
                    body: JSON.stringify({ campaign_path: selectedCampaign.value }),
                });
                const d = await r.json().catch(() => ({}));
                if (!r.ok) {
                    const msg = formatCampaignImportError(d.detail || d.message, '重新识别接口返回错误');
                    campaignImportErrorText.value = msg;
                    throw new Error(msg);
                }
                if (d.status !== 'accepted' || !d.job_id) {
                    const msg = formatCampaignImportError(d.detail || d.message, '重新识别接口返回错误');
                    campaignImportErrorText.value = msg;
                    throw new Error(msg);
                }
                campaignImportJobId.value = d.job_id;
                syncCampaignImportJob(d.job);
                const job = await pollCampaignImportJob(d.job_id);
                const result = job.result || {};
                const imported = await verifyCampaignImportVisible(result);
                const importedName = imported.name || result.name || '当前剧本';
                const importedPath = imported.path || result.campaign_path || selectedCampaign.value;
                showCampaignImportOutcome({
                    ok: true,
                    title: '剧本重新识别成功',
                    message: `已重新生成「${importedName}」的场景、角色、地图与分段知识库，并确认仍在启动页列表中。`,
                    path: importedPath,
                    stats: {
                        scenes: result.nodes_count || 0,
                        characters: result.characters_count || 0,
                        lore: result.lore_count || 0,
                        entities: result.entities_count || 0,
                        rooms: result.map_rooms_count || 0,
                        knowledge: result.knowledge_count || 0,
                        assets: result.assets_count || 0,
                    },
                    warnings: job.warnings || [],
                });
                showCampaignImportVerifiedNotice({
                    title: '重新识别成功',
                    message: `「${importedName}」已更新，可重新载入游玩。`,
                    path: importedPath,
                });
            } catch(e) {
                const msg = formatCampaignImportError(e?.message, '重新识别失败');
                campaignImportErrorText.value = msg;
                showCampaignImportOutcome({
                    ok: false,
                    title: '剧本重新识别失败',
                    message: msg,
                    warnings: campaignImportWarnings.value || [],
                });
            } finally {
                campaignImportBusy.value = false;
            }
        };
        // ── AI 模型管理 ──
        const fetchAiModels = async (openOnSuccess = false) => {
            if (isFetchingAiModels.value) return;
            isFetchingAiModels.value = true;
            aiModelError.value = '';
            try {
                const r = await fetch(`${API_BASE_URL}/api/ai/models`, { headers: accountHeaders() });
                const d = await r.json();
                if (d.status === 'success') {
                    const currentModel = d.active || apiKeyInputs.value.chatModel || '';
                    aiModels.value = ensureModelOption(d.models || [], currentModel, d.provider);
                    syncConfigModelOptions(d.models || [], { provider: d.provider });
                    syncChatModelSelection(currentModel, { models: aiModels.value, provider: d.provider });
                    aiModelSearch.value = '';
                    openAiModelDropdownState.value = openOnSuccess && !!aiModels.value.length;
                    if (d.error) aiModelError.value = `获取模型失败：${d.error}`;
                    if (d.provider) {
                        activeAiProvider.value = {
                            id: d.provider.id,
                            name: d.provider.name,
                            base_url: d.provider.base_url,
                        };
                        activeProviderId.value = activeProviderId.value || d.provider.id || '';
                    }
                } else {
                    aiModelError.value = d.message || '获取模型失败';
                }
            } catch(e) {
                aiModelError.value = '获取模型失败，请检查后端是否运行';
            } finally {
                isFetchingAiModels.value = false;
            }
        };
        const openAiModelDropdown = () => {
            if (aiModels.value.length) openAiModelDropdownState.value = true;
        };
        const toggleAiModelDropdown = () => {
            openAiModelDropdownState.value = !openAiModelDropdownState.value;
        };
        const filteredAiModels = () => {
            const q = aiModelSearch.value.trim().toLowerCase();
            const models = aiModels.value || [];
            if (!q) return models;
            return models.filter(m => `${m.label || ''} ${m.key || ''} ${m.model_id || ''} ${m.provider || ''}`.toLowerCase().includes(q));
        };
        const selectAiModel = async (modelKey) => {
            await switchAiModel(modelKey);
            openAiModelDropdownState.value = false;
        };
        const selectFirstFilteredAiModel = () => {
            const first = filteredAiModels().find(m => m.available);
            if (first?.key) selectAiModel(first.key);
        };
        const applyAiModelDraft = async () => {
            const target = aiModelDraft.value.trim();
            if (!target) {
                aiModelDraft.value = activeAiModel.value;
                return;
            }
            await switchAiModel(target);
            openAiModelDropdownState.value = false;
        };
        const switchAiModel = async (modelKey) => {
            const targetModel = (modelKey || '').trim();
            if (!targetModel) return;
            if (!multiplayerAuthAccount.value) {
                alert('请先登录账号');
                return;
            }
            if (targetModel === activeAiModel.value) {
                syncChatModelSelection(targetModel);
                return;
            }
            try {
                const r = await fetch(`${API_BASE_URL}/api/ai/models/switch`, {
                    method: 'POST', headers: accountHeaders(true),
                    body: JSON.stringify({ model: targetModel })
                });
                const d = await r.json();
                if (d.status === 'success') {
                    syncChatModelSelection(d.active || targetModel);
                    await fetchApiKeyStatus();
                } else {
                    alert(d.message || '切换失败');
                }
            } catch(e) { alert('切换模型失败，请检查后端是否运行'); }
        };
        const _modelMeta = (key) => aiModels.value.find(m => m.key === key) || {};
        const _isActiveProviderModel = (key) => {
            const meta = _modelMeta(key);
            return !meta.provider_id || !activeProviderId.value || meta.provider_id === activeProviderId.value;
        };
        const modelAccentColor = (key) => _isActiveProviderModel(key) ? '#4fc98a' : '#38bdf8';
        const modelAccentRgb   = (key) => _isActiveProviderModel(key) ? '79,201,138' : '56,189,248';
        const modelIcon        = (key) => _isActiveProviderModel(key) ? 'ph ph-check-circle' : 'ph ph-cpu';
        const cycleModel = () => {
            const available = aiModels.value.filter(m => m.available).map(m => m.key);
            if (!available.length) return;
            const idx = available.indexOf(activeAiModel.value);
            switchAiModel(available[(idx + 1) % available.length]);
        };
        const persistClearedSoloSelection = () => {
            try {
                localStorage.setItem('zric-solo-profile', JSON.stringify({
                    name: soloPlayerName.value || '玩家',
                    characterIds: [],
                }));
            } catch (_) {}
        };
        const createSoloSessionId = () => `solo-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
        const normalizeSoloCharacterSelectionForCurrentCampaign = (ids) => {
            const normalized = normalizeSoloCharacterSelection(ids);
            if (!normalized.length || !characters.value.length) return normalized;
            const playableIds = new Set(soloPlayableCharacters.value.map(c => String(c.id)));
            return normalized.filter(id => playableIds.has(String(id)));
        };
        const buildSoloSessionState = (base = {}, options = {}) => {
            const currentSelected = normalizeSoloCharacterSelection(soloSelectedCharacterIds.value);
            const currentConfirmed = normalizeSoloCharacterSelection(soloConfirmedCharacterIds.value);
            const currentActionLog = (soloActionLog.value || []).slice(-SOLO_SESSION_LOG_LIMIT);
            const preserveSessionState = !!options.preserveSessionState;
            return {
                ...base,
                playerName: preserveSessionState
                    ? (base.playerName || soloPlayerName.value || '玩家')
                    : (soloPlayerName.value || base.playerName || '玩家'),
                selectedCharacterIds: preserveSessionState
                    ? normalizeSoloCharacterSelection(base.selectedCharacterIds || base.confirmedCharacterIds || [])
                    : currentSelected,
                confirmedCharacterIds: preserveSessionState
                    ? normalizeSoloCharacterSelection(base.confirmedCharacterIds || [])
                    : currentConfirmed,
                actionLog: preserveSessionState && Array.isArray(base.actionLog)
                    ? base.actionLog.slice(-SOLO_SESSION_LOG_LIMIT)
                    : currentActionLog,
                currentNodeId: currentNode.value?.id || base.currentNodeId || null,
                updatedAt: Date.now(),
            };
        };
        const currentSoloSessionExportState = () => ({
            playerName: soloPlayerName.value || '玩家',
            selectedCharacterIds: normalizeSoloCharacterSelection(soloSelectedCharacterIds.value),
            confirmedCharacterIds: normalizeSoloCharacterSelection(soloConfirmedCharacterIds.value),
            actionLog: (soloActionLog.value || []).slice(-SOLO_SESSION_LOG_LIMIT),
            currentNodeId: currentNode.value?.id || null,
        });
        const readSoloSessions = () => {
            const allSessions = {};
            for (const key of [SOLO_SESSIONS_KEY, ...SOLO_SESSIONS_LEGACY_KEYS]) {
                const stored = readStoredJson(key, {});
                if (stored && typeof stored === 'object' && !Array.isArray(stored)) {
                    Object.assign(allSessions, stored);
                }
            }
            return allSessions;
        };
        const writeSoloSessions = (sessions) => {
            try {
                localStorage.setItem(SOLO_SESSIONS_KEY, JSON.stringify(sessions && typeof sessions === 'object' ? sessions : {}));
            } catch (_) {}
        };
        const soloSessionRouteParamNames = ['soloSession', 'solo_session', 'solo'];
        const getSoloSessionIdFromUrl = () => {
            try {
                const url = new URL(window.location.href);
                for (const name of soloSessionRouteParamNames) {
                    const value = String(url.searchParams.get(name) || '').trim();
                    if (value) return value;
                }
                const hash = String(url.hash || '').replace(/^#/, '');
                if (hash) {
                    const hashParams = new URLSearchParams(hash.includes('?') ? hash.split('?').pop() : hash);
                    for (const name of soloSessionRouteParamNames) {
                        const value = String(hashParams.get(name) || '').trim();
                        if (value) return value;
                    }
                }
            } catch (_) {}
            return '';
        };
        const buildSoloSessionLink = (sessionId = activeSoloSessionId.value) => {
            const id = String(sessionId || '').trim();
            if (!id) return '';
            try {
                const url = new URL(window.location.href);
                soloSessionRouteParamNames.forEach(name => url.searchParams.delete(name));
                url.searchParams.set('soloSession', id);
                url.hash = '';
                return url.toString();
            } catch (_) {
                return '';
            }
        };
        const setSoloSessionRoute = (sessionId = activeSoloSessionId.value) => {
            const id = String(sessionId || '').trim();
            if (!id) return;
            try {
                const url = new URL(window.location.href);
                soloSessionRouteParamNames.forEach(name => url.searchParams.delete(name));
                url.searchParams.set('soloSession', id);
                url.hash = '';
                window.history.replaceState({}, '', url.toString());
            } catch (_) {}
        };
        const soloSessionLink = computed(() => buildSoloSessionLink(activeSoloSessionId.value));
        const restoreSoloSessionState = (session = null) => {
            const state = session && typeof session === 'object' ? session : {};
            soloPlayerName.value = String(state.playerName || soloPlayerName.value || '玩家').trim() || '玩家';
            const restoredSelected = normalizeSoloCharacterSelectionForCurrentCampaign(state.selectedCharacterIds || []);
            const restoredActionLog = Array.isArray(state.actionLog) ? state.actionLog : [];
            const restoredConfirmed = normalizeSoloCharacterSelectionForCurrentCampaign(
                state.confirmedCharacterIds?.length
                    ? state.confirmedCharacterIds
                    : (restoredSelected.length && restoredActionLog.length ? restoredSelected : [])
            );
            soloSelectedCharacterIds.value = restoredSelected.length ? restoredSelected : restoredConfirmed;
            soloConfirmedCharacterIds.value = restoredConfirmed;
            soloActionLog.value = Array.isArray(state.actionLog)
                ? state.actionLog.slice(-SOLO_SESSION_LOG_LIMIT).map(entry => ({
                    id: String(entry?.id || `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`),
                    kind: entry?.kind || 'ai',
                    sender: entry?.sender || 'AI-GM',
                    text: sanitizeSoloLogText(entry?.text || '') || '（无文本裁定）',
                    time: entry?.time || '',
                    created_at: entry?.created_at || '',
                }))
                : [];
            saveSoloProfile();
        };
        const updateSoloSessionRecord = (sessionId = activeSoloSessionId.value, extra = {}) => {
            const id = sessionId || createSoloSessionId();
            const sessions = readSoloSessions();
            const previous = sessions[id] && typeof sessions[id] === 'object' ? sessions[id] : {};
            const preserveSessionState = !!extra.preserveSessionState;
            const { preserveSessionState: _preserveSessionState, ...sessionExtra } = extra || {};
            const path = normalizeCampaignPath(
                sessionExtra.baseCampaignPath
                || sessionExtra.campaignPath
                || previous.baseCampaignPath
                || previous.campaignPath
                || selectedCampaign.value
                || loadedCampaignPath.value
                || ''
            );
            const folder = String(sessionExtra.folder || previous.folder || currentSaveFolder.value || path.split('/').pop() || '').trim();
            const roomSaveName = String(sessionExtra.roomSaveName || previous.roomSaveName || soloRoomSaveName(id)).trim();
            const roomSavePath = normalizeCampaignPath(
                sessionExtra.roomSavePath !== undefined
                    ? sessionExtra.roomSavePath
                    : (previous.roomSavePath || '')
            );
            const state = buildSoloSessionState({
                ...previous,
                ...sessionExtra,
                id,
                campaignPath: path,
                baseCampaignPath: path,
                baseCampaignName: sessionExtra.baseCampaignName || previous.baseCampaignName || path.split('/').pop() || '',
                folder,
                roomSaveName,
                roomSavePath,
                saveName: sessionExtra.saveName || previous.saveName || '',
                savePath: normalizeCampaignPath(sessionExtra.savePath || previous.savePath || ''),
            }, { preserveSessionState });
            activeSoloSessionId.value = id;
            writeSoloSessions({ ...sessions, [id]: state });
            return state;
        };
        const clearSoloSessionRoute = () => {
            activeSoloSessionId.value = '';
            try {
                const url = new URL(window.location.href);
                const hadSessionRoute = soloSessionRouteParamNames.some(name => url.searchParams.has(name));
                soloSessionRouteParamNames.forEach(name => url.searchParams.delete(name));
                if (hadSessionRoute) window.history.replaceState({}, '', url.toString());
            } catch (_) {}
        };
        const saveTimestamp = (save = {}) => {
            const raw = String(save.updated_at || '').trim();
            const parsed = Date.parse(raw.replace(' ', 'T'));
            return Number.isFinite(parsed) ? parsed : 0;
        };
        const findSaveByPath = (path = '') => {
            const normalized = normalizeCampaignPath(path);
            if (!normalized) return null;
            return saveItems.value.find(save => campaignSelectionMatches(save, normalized)) || null;
        };
        const resetLoadedCampaignSession = ({ clearSoloSelection = true } = {}) => {
            storyNodes.value = [];
            characters.value = [];
            currentNode.value = null;
            editData.value = { name: '', summary: '', content: '' };
            aiGeneratedText.value = {};
            playerAction.value = '';
            lastDynamicContext.value = null;
            optionLikelihoods.value = {};
            fateHighlightIdx.value = -1;
            generatedImageUrl.value = '';
            imgPromptUsed.value = '';
            imgLoadError.value = false;
            soloConfirmedCharacterIds.value = [];
            soloActionLog.value = [];
            if (clearSoloSelection) {
                soloSelectedCharacterIds.value = [];
                persistClearedSoloSelection();
            }
        };
        const leaveGameToMenu = () => {
            appState.value = 'menu';
            clearSoloSessionRoute();
        };
        const switchToGmSurface = () => {
            playSurface.value = 'gm';
            clearSoloSessionRoute();
        };
        const copySoloRoomLink = async (sessionId = '') => {
            const id = String(sessionId || activeSoloSessionId.value || '').trim();
            const link = buildSoloSessionLink(id);
            if (!id || !link) return;
            try {
                await navigator.clipboard.writeText(link);
                campaignLoadSummary.value = '单人房间链接已复制';
            } catch (_) {
                window.prompt('复制单人房间链接', link);
            }
        };
        const refreshLoadedCampaignResources = async (options = {}) => {
            await Promise.all([
                fetchGameState({
                    resetCurrentNode: !!options.resetCurrentNode,
                    preferredNodeId: options.preferredNodeId || null,
                }),
                fetchTimelines(),
                fetchMapData(),
                fetchLorebook(),
                fetchRagDocuments(),
                refreshCheckpointCount(),
                fetchPlayerStateBgm({ playIfPlaying: false }),
            ]);
        };
        const onCampaignSelectionChanged = () => {
            selectedCampaign.value = normalizeCampaignPath(selectedCampaign.value);
            currentSaveFolder.value = '';
            currentSavePath.value = '';
            campaignLoadSummary.value = '';
            resetLoadedCampaignSession({ clearSoloSelection: true });
        };
        const loadAndStart = async (mode = 'solo', options = {}) => {
            const targetCampaign = normalizeCampaignPath(selectedCampaign.value);
            if (!targetCampaign) return;
            if (!requireMultiplayerAuth()) return;
            const requestedSessionId = options.sessionId || activeSoloSessionId.value || getSoloSessionIdFromUrl() || createSoloSessionId();
            const existingSoloSession = mode === 'solo' ? readSoloSessions()[requestedSessionId] : null;
            const isRestoringSoloSession = !!(mode === 'solo' && options.sessionId && existingSoloSession);
            selectedCampaign.value = targetCampaign;
            pendingLaunchMode.value = mode;
            isLoading.value = true;
            isExpandingBranch.value = false;
            expandingBranchText.value = '';
            campaignLoadSummary.value = '';
            try {
                const preserveMultiplayerState = !!options.preserveMultiplayerState;
                const r = await fetch(`${API_BASE_URL}/api/game/load`, {
                    method: 'POST',
                    headers: accountHeaders(true),
                    body: JSON.stringify({
                        filename: targetCampaign,
                        preserve_multiplayer_state: preserveMultiplayerState,
                    })
                });
                const d = await r.json().catch(() => ({}));
                if (r.ok) {
                    const activateAfterLoad = options.activate !== false;
                    const loadedPath = normalizeCampaignPath(d.campaign_path || d.path || targetCampaign);
                    const basePath = normalizeCampaignPath(d.base_campaign_path || '');
                    const savePath = normalizeCampaignPath(d.current_save_path || (loadedPath.startsWith('saves/') ? loadedPath : ''));
                    const parts = loadedPath.split('/');
                    const loadedFolderName = parts[parts.length - 1] || '';
                    const saveName = String(d.current_save_name || (loadedPath.startsWith('saves/') ? loadedFolderName : '')).trim();
                    const loadedIsHiddenRoomSave = isHiddenRoomSaveName(saveName || loadedFolderName);
                    const serverSoloState = d.solo_session_state && typeof d.solo_session_state === 'object'
                        ? d.solo_session_state
                        : {};
                    const hasServerSoloState = !!(
                        normalizeSoloCharacterSelection(serverSoloState.confirmedCharacterIds || []).length
                        || normalizeSoloCharacterSelection(serverSoloState.selectedCharacterIds || []).length
                        || (Array.isArray(serverSoloState.actionLog) && serverSoloState.actionLog.length)
                    );
                    const shouldRestoreSoloState = !!(mode === 'solo' && (isRestoringSoloSession || hasServerSoloState));
                    selectedCampaign.value = basePath || (loadedPath.startsWith('campaigns/') ? loadedPath : targetCampaign);
                    loadedCampaignPath.value = loadedPath;
                    loadedCampaignName.value = d.campaign_name || loadedFolderName;
                    loadedBaseCampaignPath.value = basePath;
                    loadedBaseCampaignName.value = d.base_campaign_name || (basePath.split('/').pop() || '');
                    currentSaveFolder.value = loadedIsHiddenRoomSave ? '' : saveName;
                    currentSavePath.value = loadedIsHiddenRoomSave ? '' : savePath;
                    campaignLoadSummary.value = d.message || '剧本已载入，世界观、地图、百科库与知识库已同步';
                    if (activateAfterLoad) {
                        resetLoadedCampaignSession({ clearSoloSelection: !shouldRestoreSoloState });
                        await refreshLoadedCampaignResources({ resetCurrentNode: true, preferredNodeId: d.current_scene_id || null });
                        if (mode !== 'multiplayer') await syncMultiplayerRoomState();
                        playSurface.value = mode === 'gm' ? 'gm' : 'player';
                        appState.value = 'game';
                        if (mode === 'solo') {
                            const baseCampaignPath = normalizeCampaignPath(
                                existingSoloSession?.baseCampaignPath
                                || existingSoloSession?.campaignPath
                                || basePath
                                || (!loadedIsHiddenRoomSave && loadedPath.startsWith('campaigns/') ? loadedPath : '')
                            );
                            const restored = updateSoloSessionRecord(requestedSessionId, {
                                ...serverSoloState,
                                ...existingSoloSession,
                                preserveSessionState: shouldRestoreSoloState,
                                baseCampaignName: d.base_campaign_name || loadedBaseCampaignName.value,
                                baseCampaignPath: baseCampaignPath || basePath || selectedCampaign.value,
                                campaignPath: baseCampaignPath || basePath || selectedCampaign.value,
                                folder: existingSoloSession?.folder || serverSoloState.folder || (!loadedIsHiddenRoomSave ? (saveName || loadedFolderName) : ''),
                                saveName: loadedIsHiddenRoomSave ? '' : saveName,
                                savePath: loadedIsHiddenRoomSave ? '' : savePath,
                                roomSaveName: existingSoloSession?.roomSaveName || soloRoomSaveName(requestedSessionId),
                                roomSavePath: normalizeCampaignPath(
                                    existingSoloSession?.roomSavePath
                                    || (loadedIsHiddenRoomSave ? loadedPath : '')
                                ),
                            });
                            setSoloSessionRoute(requestedSessionId);
                            restoreSoloSessionState(restored);
                        }
                        else clearSoloSessionRoute();
                        if (mode !== 'multiplayer') showMultiplayerModal.value = false;
                    } else if (mode === 'multiplayer') {
                        clearSoloSessionRoute();
                    }
                    return d;
                } else {
                    campaignLoadSummary.value = d.message || '载入失败';
                    return null;
                }
            } catch(e) {
                campaignLoadSummary.value = '载入失败，请检查后端是否运行';
                return null;
            } finally {
                isLoading.value = false;
                pendingLaunchMode.value = '';
            }
        };
        const restoreSoloSessionFromLink = async (sessionId = getSoloSessionIdFromUrl()) => {
            if (!requireMultiplayerAuth()) return;
            const id = String(sessionId || '').trim();
            if (!id) return;
            const sessions = readSoloSessions();
            const session = sessions[id];
            if (!session || typeof session !== 'object') {
                campaignLoadSummary.value = '该单人房间链接没有本地可恢复进度';
                return;
            }
            const sessionRoomSavePath = normalizeCampaignPath(session?.roomSavePath || '');
            const sessionSavePath = normalizeCampaignPath(session?.savePath || '');
            const sessionBasePath = normalizeCampaignPath(session?.baseCampaignPath || session?.campaignPath || '');
            const sessionPath = normalizeCampaignPath(sessionRoomSavePath || sessionSavePath || sessionBasePath || '');
            if (!sessionPath) {
                campaignLoadSummary.value = '该单人房间链接没有可载入的进度';
                return;
            }
            selectedCampaign.value = sessionPath;
            await loadAndStart('solo', { sessionId: id });
        };
        const autoRestoreSoloSession = async () => {
            if (!multiplayerAuthAccount.value || !multiplayerAuthToken.value) return;
            const urlSessionId = getSoloSessionIdFromUrl();
            if (!urlSessionId) return;
            if (appState.value === 'game' && playSurface.value === 'player' && activeSoloSessionId.value === urlSessionId) return;
            await restoreSoloSessionFromLink(urlSessionId);
        };
        const formatSaveSize = (bytes) => {
            const n = Number(bytes || 0);
            if (!n) return '';
            if (n < 1024) return `${n} B`;
            if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
            return `${(n / 1024 / 1024).toFixed(1)} MB`;
        };
        const saveStatsText = (save) => {
            if (!save) return '';
            const parts = [];
            if (save.updated_at) parts.push(save.updated_at);
            if (Number.isFinite(Number(save.node_count))) parts.push(`${save.node_count} 场景`);
            if (Number(save.kb_count || save.rag_count || 0)) parts.push(`${save.kb_count || save.rag_count} 知识`);
            const size = formatSaveSize(save.size_bytes);
            if (size) parts.push(size);
            return parts.join(' / ') || save.path || '';
        };
        const exportSave = async () => {
            exportShowNameInput.value = false;
            exportNewName.value = '';
            saveSearch.value = '';
            saveManagerMsg.value = '';
            showExportModal.value = true;
            await fetchSaves();
        };
        const doExportSave = async (saveName, options = {}) => {
            if (isExportingSave.value) return null;
            if (!requireMultiplayerAuth()) return null;
            const cleanSaveName = String(saveName || '').trim();
            isExportingSave.value = true;
            saveManagerMsg.value = '';
            try {
                if (currentNode.value?.id) await syncPlayerStateBgm({ current_scene_id: currentNode.value.id });
                const baseCampaignPath = normalizeCampaignPath(
                    options.baseCampaignPath
                    || lockedSaveSourcePath.value
                    || selectedCampaign.value
                    || loadedCampaignPath.value
                    || ''
                );
                const headers = accountHeaders(true);
                const hiddenRoomCode = String(options.roomCode || '').trim().toUpperCase()
                    || multiplayerRoomCodeFromSave({ name: cleanSaveName })
                    || (options.hiddenRoomSave ? String(multiplayerRoom.value?.code || '').trim().toUpperCase() : '');
                if (hiddenRoomCode && multiplayerRoomTokens.value[hiddenRoomCode]) {
                    headers['X-Room-Token'] = multiplayerRoomTokens.value[hiddenRoomCode];
                }
                const r = await fetch(`${API_BASE_URL}/api/game/export`, {
                    method: 'POST',
                    headers,
                    body: JSON.stringify({
                        save_name: cleanSaveName,
                        room_code: hiddenRoomCode || '',
                        base_campaign_path: baseCampaignPath,
                        solo_session_state: playSurface.value === 'player' ? currentSoloSessionExportState() : {},
                    }),
                });
                const d = await r.json().catch(() => ({}));
                if (!r.ok || d.status !== 'success') throw new Error(d.message || d.detail || '保存失败');
                const hiddenRoomSave = !!options.hiddenRoomSave || isHiddenRoomSaveName(d.folder || saveName || '');
                if (!hiddenRoomSave) {
                    currentSaveFolder.value = d.folder || cleanSaveName || currentSaveFolder.value;
                    currentSavePath.value = normalizeCampaignPath(d.path || currentSavePath.value);
                    loadedBaseCampaignPath.value = normalizeCampaignPath(d.base_campaign_path || loadedBaseCampaignPath.value || baseCampaignPath);
                    loadedBaseCampaignName.value = d.base_campaign_name || loadedBaseCampaignName.value || (loadedBaseCampaignPath.value.split('/').pop() || '');
                }
                if (!hiddenRoomSave && activeSoloSessionId.value && playSurface.value === 'player') {
                    updateSoloSessionRecord(activeSoloSessionId.value, {
                        baseCampaignName: d.base_campaign_name || loadedBaseCampaignName.value,
                        baseCampaignPath: d.base_campaign_path || baseCampaignPath,
                        campaignPath: d.base_campaign_path || baseCampaignPath,
                        folder: currentSaveFolder.value,
                        saveName: d.folder || cleanSaveName || currentSaveFolder.value,
                        savePath: d.path || currentSavePath.value,
                    });
                }
                if (!hiddenRoomSave) await fetchSaves();
                saveManagerOk.value = true;
                saveManagerMsg.value = d.message || '存档已保存';
                return d;
            } catch(e) {
                saveManagerOk.value = false;
                saveManagerMsg.value = e?.message || '保存失败';
                return null;
            } finally {
                isExportingSave.value = false;
            }
        };
        const saveSoloRoomProgress = async (sessionId = activeSoloSessionId.value, options = {}) => {
            if (!sessionId || playSurface.value !== 'player' || appState.value !== 'game') return null;
            const existing = readSoloSessions()[sessionId] || {};
            const saveName = String(options.roomSaveName || existing.roomSaveName || soloRoomSaveName(sessionId)).trim();
            if (!saveName) return null;
            const result = await doExportSave(saveName, { hiddenRoomSave: true });
            if (!result) return null;
            updateSoloSessionRecord(sessionId, {
                roomSaveName: result.folder || saveName,
                roomSavePath: result.path || '',
                baseCampaignPath: existing?.baseCampaignPath || existing?.campaignPath || selectedCampaign.value || '',
            });
            campaignLoadSummary.value = '单人房间进度已保存';
            return result;
        };
        const persistSoloSessionProgress = async (sessionId = activeSoloSessionId.value, options = {}) => {
            if (!sessionId || playSurface.value !== 'player' || appState.value !== 'game') return null;
            updateSoloSessionRecord(sessionId, options);
            return saveSoloRoomProgress(sessionId, options);
        };
        const persistSoloSessionSnapshot = () => {
            if (activeSoloSessionId.value && playSurface.value === 'player') {
                updateSoloSessionRecord(activeSoloSessionId.value);
            }
        };
        const saveCurrentMultiplayerRoomProgress = async () => {
            const room = multiplayerRoom.value;
            if (!room?.code) return null;
            const saveName = multiplayerRoomSaveName(room.code);
            if (!saveName) return null;
            const result = await doExportSave(saveName, { hiddenRoomSave: true, roomCode: room.code });
            if (!result) return null;
            try {
                const currentSettings = room.settings && typeof room.settings === 'object' ? room.settings : {};
                const data = await multiplayerApi(`/api/multiplayer/rooms/${encodeURIComponent(room.code)}`, {
                    method: 'PATCH',
                    body: JSON.stringify({
                        settings: {
                            ...currentSettings,
                            gm_console: true,
                            campaign_path: currentSettings.campaign_path || selectedCampaign.value || loadedCampaignPath.value || '',
                            room_save_name: result.folder || saveName,
                            room_save_path: result.path || '',
                        },
                    }),
                });
                applyMultiplayerSnapshot(data);
                multiplayerStatusMsg.value = `房间 ${room.code} 进度已保存`;
            } catch (e) {
                multiplayerStatusMsg.value = e?.message || '房间进度保存后同步失败';
            }
            return result;
        };
        const canSaveCurrentMultiplayerRoom = computed(() => {
            const code = String(multiplayerRoom.value?.code || '').trim();
            return !!(code && (multiplayerRoomTokens.value[code] || multiplayerRoom.value?.can_manage));
        });
        const doExportOverwrite = async () => {
            if (canSaveCurrentMultiplayerRoom.value) {
                await saveCurrentMultiplayerRoomProgress();
                return;
            }
            if (!currentSaveWritable.value) {
                saveManagerOk.value = false;
                saveManagerMsg.value = currentSaveFolder.value
                    ? '当前剧本不是你的可覆盖存档，请新建个人存档保存当前进度'
                    : '当前未绑定存档，请先新建个人存档';
                return;
            }
            await doExportSave(currentSaveFolder.value);
        };
        const doExportNew = async () => {
            const name = exportNewName.value.trim();
            const result = await doExportSave(name);
            if (result) exportNewName.value = '';
        };
        const downloadSave = async (save) => {
            if (!save?.download_url) return;
            if (!requireMultiplayerAuth()) return;
            try {
                const r = await fetch(`${API_BASE_URL}${save.download_url}`, { headers: accountHeaders() });
                if (!r.ok) {
                    const d = await r.json().catch(() => ({}));
                    throw new Error(d.detail || d.message || '下载失败');
                }
                const blob = await r.blob();
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = `${save.name || 'zric-save'}.zip`;
                document.body.appendChild(a);
                a.click();
                a.remove();
                URL.revokeObjectURL(url);
            } catch (e) {
                saveManagerOk.value = false;
                saveManagerMsg.value = e?.message || '下载失败';
            }
        };
        const deleteSave = async (save) => {
            if (!requireMultiplayerAuth()) return;
            const ref = saveIdentity(save);
            if (!save?.deletable || !ref) return;
            if (!confirm(`删除存档「${save.name}」？`)) return;
            try {
                const r = await fetch(`${API_BASE_URL}/api/game/saves/${encodeURIComponent(ref)}`, { method: 'DELETE', headers: accountHeaders() });
                const d = await r.json().catch(() => ({}));
                if (!r.ok || d.status !== 'success') throw new Error(d.message || d.detail || '删除失败');
                if (currentSaveFolder.value === save.name || currentSavePath.value === normalizeCampaignPath(save.path)) {
                    currentSaveFolder.value = '';
                    currentSavePath.value = '';
                }
                await fetchSaves();
                saveManagerOk.value = true;
                saveManagerMsg.value = d.message || '存档已删除';
            } catch(e) {
                saveManagerOk.value = false;
                saveManagerMsg.value = e?.message || '删除失败';
            }
        };
        const startRenameSave = (save) => {
            if (!save?.renamable || !save.name) return;
            renamingSaveName.value = saveIdentity(save);
            renameSaveDraft.value = save.name;
            saveManagerMsg.value = '';
        };
        const cancelRenameSave = () => {
            renamingSaveName.value = '';
            renameSaveDraft.value = '';
        };
        const renameSave = async (save) => {
            if (!requireMultiplayerAuth()) return;
            const ref = saveIdentity(save);
            if (!save?.renamable || !ref || isRenamingSave.value) return;
            const newName = renameSaveDraft.value.trim();
            isRenamingSave.value = true;
            saveManagerMsg.value = '';
            try {
                const oldName = save.name;
                const oldPath = save.path;
                const r = await fetch(`${API_BASE_URL}/api/game/saves/${encodeURIComponent(ref)}/rename`, {
                    method: 'PATCH',
                    headers: accountHeaders(true),
                    body: JSON.stringify({ new_name: newName }),
                });
                const d = await r.json().catch(() => ({}));
                if (!r.ok || d.status !== 'success') throw new Error(d.message || d.detail || '重命名失败');
                const renamedName = d.name || newName;
                const renamedPath = normalizeCampaignPath(d.path || oldPath);
                if (currentSaveFolder.value === oldName || currentSavePath.value === normalizeCampaignPath(oldPath)) {
                    currentSaveFolder.value = renamedName;
                    currentSavePath.value = renamedPath;
                }
                if (loadedCampaignPath.value === normalizeCampaignPath(oldPath) || loadedCampaignName.value === oldName) {
                    loadedCampaignName.value = renamedName;
                    loadedCampaignPath.value = renamedPath;
                }
                if (activeSoloSessionId.value && currentSaveFolder.value === renamedName) {
                    updateSoloSessionRecord(activeSoloSessionId.value, {
                        baseCampaignName: save.campaign_name || loadedBaseCampaignName.value,
                        baseCampaignPath: save.campaign_path || loadedBaseCampaignPath.value,
                        campaignPath: save.campaign_path || loadedBaseCampaignPath.value,
                        folder: renamedName,
                        saveName: renamedName,
                        savePath: renamedPath,
                    });
                }
                await fetchSaves();
                saveManagerOk.value = true;
                saveManagerMsg.value = d.message || '存档已重命名';
                cancelRenameSave();
            } catch(e) {
                saveManagerOk.value = false;
                saveManagerMsg.value = e?.message || '重命名失败';
            } finally {
                isRenamingSave.value = false;
            }
        };
        const loadSaveFromManager = async (save) => {
            if (!save?.path) return;
            if (savePlayMode(save) === 'multiplayer') {
                await restoreMultiplayerSaveAsRoom(save);
                return;
            }
            selectedCampaign.value = normalizeCampaignPath(save.path);
            saveManagerMsg.value = '';
            const mode = playSurface.value === 'gm' ? 'gm' : 'solo';
            const session = mode === 'solo' ? findSoloSessionForSave(save) : null;
            await loadAndStart(mode, session?.id ? { sessionId: session.id } : {});
            if (campaignLoadSummary.value && campaignLoadSummary.value.includes('失败')) {
                saveManagerOk.value = false;
                saveManagerMsg.value = campaignLoadSummary.value;
                return;
            }
            currentSaveFolder.value = save.name || save.path.split('/').pop();
            currentSavePath.value = normalizeCampaignPath(save.path);
            saveManagerOk.value = true;
            saveManagerMsg.value = `已载入 ${save.name || save.path}`;
            showExportModal.value = false;
        };
        const findSoloSessionForSave = (save) => {
            const savePath = normalizeCampaignPath(save?.path || '');
            if (!savePath) return null;
            const sessions = readSoloSessions();
            return Object.values(sessions)
                .filter(session => session && typeof session === 'object')
                .filter(session => {
                    const directSavePath = normalizeCampaignPath(session.savePath || '');
                    const roomSavePath = normalizeCampaignPath(session.roomSavePath || '');
                    return directSavePath === savePath || roomSavePath === savePath;
                })
                .sort((a, b) => Number(b.updatedAt || 0) - Number(a.updatedAt || 0))[0] || null;
        };
        const restoreMultiplayerSaveAsRoom = async (save) => {
            const savePath = normalizeCampaignPath(save?.path || '');
            if (!savePath) return;
            if (!requireMultiplayerAuth()) return;
            if (multiplayerWs) multiplayerWs.close();
            multiplayerWs = null;
            multiplayerWsConnected.value = false;
            multiplayerRoom.value = null;
            multiplayerMembers.value = [];
            multiplayerMessages.value = [];
            multiplayerError.value = '';
            multiplayerStatusMsg.value = `正在恢复 ${saveDisplayName(save)}...`;
            showMultiplayerModal.value = true;
            selectedCampaign.value = savePath;
            const loaded = await loadAndStart('multiplayer', {
                activate: false,
                preserveMultiplayerState: true,
            });
            if (campaignLoadSummary.value && campaignLoadSummary.value.includes('失败')) {
                saveManagerOk.value = false;
                saveManagerMsg.value = campaignLoadSummary.value;
                multiplayerError.value = campaignLoadSummary.value;
                return;
            }
            const roomSavePath = normalizeCampaignPath(loadedCampaignPath.value || savePath);
            const savePathParts = savePath.split('/');
            const inferredCampaignPath = savePathParts[0] === 'saves' && savePathParts[1]
                ? `campaigns/${savePathParts[1]}`
                : '';
            const campaignPath = normalizeCampaignPath(
                save.campaign_path
                || loadedBaseCampaignPath.value
                || inferredCampaignPath
                || (normalizeCampaignPath(selectedCampaign.value).startsWith('campaigns/') ? selectedCampaign.value : '')
                || ''
            );
            const restoredName = saveDisplayName(save);
            multiplayerRoomName.value = restoredName || multiplayerRoomName.value || '恢复的多人房间';
            const restoreState = loaded?.multiplayer_room_state || {};
            const restoredRoomCode = multiplayerRoomCodeFromSave(save)
                || multiplayerRoomCodeFromSave({ name: loaded?.current_save_name || roomSavePath.split('/').pop() || '' })
                || String(restoreState.source_room_code || '').trim().toUpperCase();
            let data = null;
            if (restoredRoomCode) {
                const currentSettings = restoreState.room?.settings && typeof restoreState.room.settings === 'object'
                    ? restoreState.room.settings
                    : {};
                try {
                    data = await multiplayerApi(`/api/multiplayer/rooms/${encodeURIComponent(restoredRoomCode)}`, {
                        method: 'PATCH',
                        body: JSON.stringify({
                            name: multiplayerRoomName.value,
                            settings: {
                                ...currentSettings,
                                gm_console: true,
                                campaign_path: campaignPath,
                                room_save_path: roomSavePath,
                                room_save_name: roomSavePath.split('/').pop() || '',
                                restore_state: restoreState,
                            },
                        }),
                    });
                } catch (err) {
                    if (!/不存在|404/.test(String(err?.message || ''))) throw err;
                }
            }
            if (!data) {
                data = await createMultiplayerRoom({
                    name: multiplayerRoomName.value,
                    campaignPath,
                    roomSavePath,
                    restoreState,
                    restoreCode: restoredRoomCode,
                });
            }
            if (!data?.room?.code) {
                saveManagerOk.value = false;
                saveManagerMsg.value = multiplayerError.value || '创建恢复房间失败';
                return;
            }
            saveManagerOk.value = true;
            saveManagerMsg.value = `已恢复多人存档，正在进入房间 ${data.room.code}`;
            multiplayerStatusMsg.value = saveManagerMsg.value;
            showExportModal.value = false;
            window.location.replace(multiplayerTableUrl(data.room.code));
        };
        const restoreSaveFromMenu = async (save) => {
            if (!save?.path) return;
            if (savePlayMode(save) === 'multiplayer') {
                await restoreMultiplayerSaveAsRoom(save);
                return;
            }
            const session = findSoloSessionForSave(save);
            selectedCampaign.value = normalizeCampaignPath(save.path);
            await loadAndStart('solo', { sessionId: session?.id || createSoloSessionId() });
        };

        // ── Game State ──
        const syncEditData = () => { if(currentNode.value) editData.value={...currentNode.value}; };
        const applyNodeSceneImage = (node) => {
            generatedImageUrl.value = node?.scene_image || '';
            imgPromptUsed.value = node?.scene_image ? '导入剧本资源' : '';
            imgLoadError.value = false;
        };
        const soloPlayableCharacters = computed(() => {
            const active = characters.value.filter(c => (c.status || 'active') !== 'hidden');
            return active.length ? active : characters.value;
        });
        const soloSelectedCharacters = computed(() => {
            const selectedIds = new Set(soloSelectedCharacterIds.value.map(id => String(id)));
            return characters.value.filter(c => selectedIds.has(String(c.id)));
        });
        const soloConfirmedCharacters = computed(() => {
            const confirmedIds = new Set(soloConfirmedCharacterIds.value.map(id => String(id)));
            return characters.value.filter(c => confirmedIds.has(String(c.id)));
        });
        const soloSelectedNames = computed(() => soloSelectedCharacters.value.map(c => c.name));
        const soloConfirmedNames = computed(() => soloConfirmedCharacters.value.map(c => c.name));
        const soloHasConfirmedCharacters = computed(() => soloConfirmedCharacterIds.value.length > 0);
        const soloActorLabel = computed(() => {
            const names = soloHasConfirmedCharacters.value ? soloConfirmedNames.value : soloSelectedNames.value;
            return names.join(' / ') || soloPlayerName.value || '玩家';
        });
        const soloVisibleCharacters = computed(() => {
            if (soloHasConfirmedCharacters.value) return soloConfirmedCharacters.value;
            if (soloSelectedCharacterIds.value.length) return soloSelectedCharacters.value;
            return soloPlayableCharacters.value;
        });
        const compactText = (value, max = 220) => String(value || '').replace(/\s+/g, ' ').trim().slice(0, max);
        const isInternalSceneName = (name = '') => /守秘人|GM|KP|幕后|真相|后台|导入|索引|规则说明|系统信息/i.test(String(name || ''));
        const sceneLooksPlayerVisible = (node = {}) => {
            if (!node || isInternalSceneName(node.name)) return false;
            const sample = compactText(`${node.summary || ''} ${node.content || ''}`, 700);
            return !/(?:守秘人|KP|GM|主持|英雄们|模组|危险的谎言|邪恶的超自然力量|玩家们|NPC角色)/i.test(sample);
        };
        const firstPlayerVisibleNode = () => storyNodes.value.find(sceneLooksPlayerVisible) || storyNodes.value[0] || null;
        const cleanCharacterBriefText = (value = '') => String(value || '')
            .replace(/原始身份：剧本主要\s*(?:登场\s*)?NPC，可作为玩家扮演角色。[；;\s]*/g, '')
            .replace(/原始身份：剧本主要\s*(?:登场\s*)?NPC[，,。；;\s]*/g, '')
            .replace(/可作为玩家扮演角色。[；;\s]*/g, '')
            .replace(/【(?:AI|GM|KP|系统|推演|判定|规则系统|信息隔离|时间流速|禁止事项|基本设定|核心规则|导入|编辑|后台)[^】]*】[\s\S]*?(?=\n【|$)/gi, '')
            .split(/(?:^|\s)(?:HP|SAN|MP|生命值|理智值)\s*(?:代表|[:：])/i)[0]
            .split(/(?:AI|GM|KP|RAG|knowledge\/|world_entities|lorebook|source_markdown|embedding|推演约束|系统提示|后台|知识库|核心规则|基本设定|禁止事项|HP\s*(?:代表|[:：])|SAN\s*(?:代表|[:：])|MP\s*(?:代表|[:：])|生命值|理智值|好感度)/i)[0]
            .replace(/([。！？；，、,.!?;])\1+/g, '$1')
            .replace(/\s+/g, ' ')
            .trim()
            .replace(/[。；;]+$/g, '');
        const briefWithoutNamePrefix = (value = '', name = '') => {
            const escaped = String(name || '').replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
            let text = cleanCharacterBriefText(value);
            if (escaped) {
                text = text
                    .replace(new RegExp(`^${escaped}\\s*[：:]\\s*`), '')
                    .replace(new RegExp(`^${escaped}\\s*[（(][^）)]*[）)]\\s*[：:。]\\s*`), '')
                    .replace(new RegExp(`^${escaped}\\s*(?:与当前事件有直接关联。)?`), '');
            }
            return text
                .replace(/入场(?:后|时).*?(?:不需要预设唯一正确选择|判断下一步)[。；;]?/g, '')
                .trim()
                .replace(/[。；;]+$/g, '');
        };
        const inventoryBriefText = (value = '') => cleanCharacterBriefText(value)
            .replace(/^(?:身份|持有|物品|资源)[:：]\s*/i, '')
            .trim()
            .replace(/[。；;]+$/g, '');
        const characterSeed = (char, extra = '') => {
            const source = `${char?.id || ''}|${char?.name || ''}|${char?.role || ''}|${char?.personality || ''}|${char?.inventory || ''}|${extra}`;
            return Array.from(source).reduce((sum, ch, idx) => sum + ch.charCodeAt(0) * (idx + 1), 0);
        };
        const characterEntryBriefing = (char, sceneName, publicText) => {
            if (!char) return '';
            const name = char.name || '角色';
            const role = char.role || '角色';
            const roleSuffix = ['PC', 'NPC'].includes(String(role || '').toUpperCase()) ? '' : `（${role}）`;
            const scene = sceneName || '开场';
            const personality = cleanCharacterBriefText(char.personality);
            const roleBrief = briefWithoutNamePrefix(char.role_brief, name) || briefWithoutNamePrefix(personality, name);
            const scriptBrief = cleanCharacterBriefText(char.script_brief);
            const openingPrompt = cleanCharacterBriefText(char.opening_prompt);
            const inventory = inventoryBriefText(char.inventory);
            const worldviewBrief = compactText(cleanCharacterBriefText(worldviewContent.value), 780);
            const sceneBrief = compactText(publicText, 420) || '当前场景细节尚未完全展开。';
            const scriptIntro = scriptBrief || worldviewBrief || sceneBrief;
            const lines = [
                `剧本简介：${compactText(scriptIntro, 900)}`,
                `当前开局：${sceneBrief}`,
                `角色背景：${name}${roleSuffix}。${roleBrief ? compactText(roleBrief, 700) : '需要根据现场信息判断自己的立场、风险和可行动资源。'}`,
            ];
            if (inventory) lines.push(`可用资源：${compactText(inventory, 300)}`);
            lines.push(`开场白：${openingPrompt ? compactText(openingPrompt, 500) : `${name}已经进入「${scene}」。先确认当前目标、可互动对象、关键线索和最紧迫的风险，再结合自身身份与资源决定下一步行动。`}`);
            return lines.join('\n');
        };
        const characterPerspectiveOptions = (char, options, sceneText = '') => {
            const list = Array.isArray(options) ? options : [];
            if (!char) return list.map(opt => ({ ...opt, visible_text: opt.text, action_text: opt.text }));
            return list.map((opt, idx) => {
                const rawText = compactText(opt?.text, 180) || '自由行动';
                const name = char.name || '角色';
                const lenses = [
                    `${name}先${rawText}`,
                    `${name}观察周围反应后，${rawText}`,
                    `${name}带着当前顾虑，尝试${rawText}`,
                    `${name}确认眼前风险后，${rawText}`,
                    `${name}抓住当前机会，${rawText}`,
                ];
                const visibleText = lenses[(characterSeed(char, `${idx}|${rawText}|${sceneText}`) + idx) % lenses.length];
                return {
                    ...opt,
                    visible_text: visibleText,
                    action_text: visibleText,
                };
            });
        };
        const soloPerspectiveCharacter = computed(() => {
            if (soloHasConfirmedCharacters.value) return soloConfirmedCharacters.value[0] || null;
            return soloSelectedCharacters.value[0] || null;
        });
        const soloPublicSceneText = computed(() => {
            const node = sceneLooksPlayerVisible(currentNode.value) ? (currentNode.value || {}) : (firstPlayerVisibleNode() || currentNode.value || {});
            return node.expanded_content || node.content || '';
        });
        const soloSceneText = computed(() => {
            return soloHasConfirmedCharacters.value ? soloPublicSceneText.value : compactText(soloPublicSceneText.value, 520);
        });
        const soloCharacterBriefingText = computed(() => {
            const selected = soloHasConfirmedCharacters.value ? soloConfirmedCharacters.value : soloSelectedCharacters.value;
            const briefingNode = sceneLooksPlayerVisible(currentNode.value) ? currentNode.value : firstPlayerVisibleNode();
            return selected
                .map(char => characterEntryBriefing(char, briefingNode?.name || '开场', soloPublicSceneText.value))
                .filter(Boolean)
                .join('\n\n');
        });
        const showSoloCharacterBriefing = computed(() => {
            return soloHasConfirmedCharacters.value && !soloActionLog.value.length && !!soloCharacterBriefingText.value;
        });
        const soloVisibleOptions = computed(() => characterPerspectiveOptions(
            soloPerspectiveCharacter.value,
            currentNode.value?.options || [],
            soloPublicSceneText.value
        ));
        const saveSoloProfile = () => {
            localStorage.setItem('zric-solo-profile', JSON.stringify({
                name: soloPlayerName.value || '玩家',
                characterIds: normalizeSoloCharacterSelection(soloSelectedCharacterIds.value),
            }));
            persistSoloSessionSnapshot();
        };
        const isSoloCharacterSelected = (id) => {
            const idKey = String(id);
            return soloSelectedCharacterIds.value.some(selectedId => String(selectedId) === idKey);
        };
        const isSoloCharacterLocked = (id) => {
            const idKey = String(id);
            return soloConfirmedCharacterIds.value.some(confirmedId => String(confirmedId) === idKey);
        };
        const clearSoloCharacters = () => {
            if (soloHasConfirmedCharacters.value) return;
            soloSelectedCharacterIds.value = [];
            saveSoloProfile();
        };
        const confirmSoloCharacters = () => {
            if (soloHasConfirmedCharacters.value || !soloSelectedCharacterIds.value.length) return;
            soloConfirmedCharacterIds.value = normalizeSoloCharacterSelection(soloSelectedCharacterIds.value);
            saveSoloProfile();
            if (activeSoloSessionId.value) updateSoloSessionRecord(activeSoloSessionId.value);
        };
        const toggleSoloCharacter = (id) => {
            if (soloHasConfirmedCharacters.value) return;
            const idKey = String(id);
            soloSelectedCharacterIds.value = isSoloCharacterSelected(id) ? [] : [id];
            saveSoloProfile();
            if (activeSoloSessionId.value) updateSoloSessionRecord(activeSoloSessionId.value);
        };
        const sanitizeSoloLogText = (value = '') => {
            const imageRefRe = /!\[[^\]]*\]\((?:\/api\/campaign-assets\/|images\/)[^)]+\)|!\[[^\]]*\]\((?:\/api\/campaign-assets\/|images\/)[^\s)]*|(?:\/api\/campaign-assets\/|images\/)[^\s)]+/gi;
            const source = String(value ?? '');
            const hadImageRef = imageRefRe.test(source) || /<details>\s*<summary>\s*(?:natural_image|text_image)\s*<\/summary>/i.test(source);
            imageRefRe.lastIndex = 0;
            const cleaned = source
                .replace(/<details>\s*<summary>\s*(?:natural_image|text_image)\s*<\/summary>[\s\S]*?<\/details>/gi, '')
                .replace(imageRefRe, '')
                .replace(/[ \t]+\n/g, '\n')
                .replace(/\n{2,}/g, '\n')
                .replace(/[ \t]{2,}/g, ' ')
                .trim();
            return cleaned || (hadImageRef ? '（图片已略过，可在场景或资料册中查看）' : '');
        };
        const addSoloLog = (kind, sender, text) => {
            const now = new Date();
            soloActionLog.value.push({
                id: `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
                kind,
                sender,
                text: sanitizeSoloLogText(text) || '（无文本裁定）',
                time: now.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }),
                created_at: now.toISOString(),
            });
            if (soloActionLog.value.length > SOLO_SESSION_LOG_LIMIT) {
                soloActionLog.value = soloActionLog.value.slice(-SOLO_SESSION_LOG_LIMIT);
            }
            nextTick(() => {
                const el = soloLogRef.value;
                if (el) el.scrollTop = 0;
            });
            if (activeSoloSessionId.value) updateSoloSessionRecord(activeSoloSessionId.value);
        };
        const summarizeAiOptionsForSolo = () => {
            const opts = currentNode.value?.options || [];
            if (!opts.length) return 'AI-GM 已记录行动，等待你继续描述下一步。';
            return 'AI-GM 给出了新的行动方向：\n' + opts.slice(0, 4).map((opt, idx) => `${idx + 1}. ${opt.text}`).join('\n');
        };
        const selectSoloVisibleOption = async (opt) => {
            if (!opt || isGeneratingOptions.value || isRollingBack.value) return;
            const nextId = Number(opt.next_node_id || 0);
            const next = storyNodes.value.find(node => Number(node.id) === nextId);
            if (!next) return;
            const actor = soloActorLabel.value;
            const visibleText = (opt.visible_text || opt.text || '').trim();
            const actionText = (opt.action_text || visibleText || opt.text || '').trim();
            addSoloLog('player', actor, visibleText || actionText || '选择行动方向');
            await jumpToNode(next.id, actionText || visibleText || opt.text || '继续');
            addSoloLog('ai', 'AI-GM', `场景推进：${next.name}\n${next.expanded_content || next.content || '等待你继续描述下一步。'}`);
            await persistSoloSessionProgress();
        };
        const fetchGameState = async (options = {}) => {
            const resetCurrentNode = !!options.resetCurrentNode;
            const preferredNodeId = options.preferredNodeId == null ? null : Number(options.preferredNodeId);
            let ok = false;
            try { const r=await fetch(`${API_BASE_URL}/api/game/state`); const d=await r.json();
                if(d.status==='success'){storyNodes.value=d.nodes;characters.value=d.characters;dbConnected.value=true;
                    ok = true;
                    loadedCampaignPath.value = normalizeCampaignPath(d.current_campaign_path || '');
                    loadedCampaignName.value = d.current_campaign_name || (loadedCampaignPath.value.split('/').pop() || '');
                    loadedBaseCampaignPath.value = normalizeCampaignPath(d.base_campaign_path || '');
                    loadedBaseCampaignName.value = d.base_campaign_name || (loadedBaseCampaignPath.value.split('/').pop() || '');
                    const stateSaveName = String(d.current_save_name || '').trim();
                    const stateSavePath = normalizeCampaignPath(d.current_save_path || '');
                    currentSaveFolder.value = stateSaveName && !isHiddenRoomSaveName(stateSaveName) ? stateSaveName : '';
                    currentSavePath.value = currentSaveFolder.value ? stateSavePath : '';
                    if(resetCurrentNode){
                        currentNode.value=null;
                        const serverNodeId = Number(d.current_scene_id || 0);
                        const resolvedPreferredId = Number.isFinite(preferredNodeId) && preferredNodeId > 0
                            ? preferredNodeId
                            : serverNodeId;
                        const target = Number.isFinite(resolvedPreferredId) && resolvedPreferredId > 0
                            ? d.nodes.find(n => Number(n.id) === resolvedPreferredId)
                            : null;
                        const next = target || firstPlayerVisibleNode() || d.nodes[0];
                        if(next){currentNode.value=next;applyNodeSceneImage(next);syncEditData();}
                    }
                    else if(currentNode.value){currentNode.value=d.nodes.find(n=>n.id===currentNode.value.id)||d.nodes[0];applyNodeSceneImage(currentNode.value);syncEditData();}
                    else if(d.nodes.length>0){
                        const serverNodeId = Number(d.current_scene_id || 0);
                        const next = d.nodes.find(n => Number(n.id) === serverNodeId) || firstPlayerVisibleNode() || d.nodes[0];
                        jumpToNode(next.id);
                    }
                }
            } catch(e){dbConnected.value=false;}
            try { const sl=await fetch(`${API_BASE_URL}/api/game/stat-labels`); const sld=await sl.json(); if(sld.hp_label)hpLabel.value=sld.hp_label; if(sld.san_label)sanLabel.value=sld.san_label; } catch(e){}
            const playableIds = new Set(soloPlayableCharacters.value.map(c => String(c.id)));
            if (soloSelectedCharacterIds.value.some(id => !playableIds.has(String(id)))) {
                soloSelectedCharacterIds.value = normalizeSoloCharacterSelection(soloSelectedCharacterIds.value.filter(id => playableIds.has(String(id))));
                saveSoloProfile();
            } else {
                soloSelectedCharacterIds.value = normalizeSoloCharacterSelection(soloSelectedCharacterIds.value);
            }
            if (soloConfirmedCharacterIds.value.some(id => !playableIds.has(String(id)))) {
                soloConfirmedCharacterIds.value = [];
            } else {
                soloConfirmedCharacterIds.value = normalizeSoloCharacterSelection(soloConfirmedCharacterIds.value);
            }
            await fetchWorldEntities();
            return ok;
        };

        // ── 流式展开分支叙事（共享工具函数）──
        // fxContext/actionType：由 applyBranchEffects 返回后回传，让 expand 读到最新状态
        const streamExpandBranch = async (nodeId, sceneName, sceneContent, playerAction, fxContext = '', actionType = '') => {
            isExpandingBranch.value = true;
            expandingBranchText.value = '';
            let fullText = '';
            try {
                const resp = await fetch(`${API_BASE_URL}/api/ai/expand-branch/stream`, {
                    method: 'POST',
                    headers: accountHeaders(true),
                    body: JSON.stringify({
                        node_id: nodeId,
                        scene_name: sceneName || '',
                        scene_content: (sceneContent || '').slice(0, 500),
                        player_action: playerAction || '',
                        fx_context: fxContext || '',
                        action_type: actionType || '',
                    })
                });
                const reader = resp.body.getReader();
                const decoder = new TextDecoder();
                while (true) {
                    const { done, value } = await reader.read();
                    if (done) break;
                    const chunk = decoder.decode(value, { stream: true });
                    for (const line of chunk.split('\n')) {
                        if (!line.startsWith('data: ')) continue;
                        try {
                            const ev = JSON.parse(line.slice(6));
                            if (ev.type === 'text') {
                                fullText += ev.content;
                                expandingBranchText.value = fullText;
                            } else if (ev.type === 'done') {
                                fullText = ev.full_text || fullText;
                                expandingBranchText.value = fullText;
                            }
                        } catch (_) {}
                    }
                }
            } catch (e) {
                console.error('stream expand-branch failed:', e);
            }
            isExpandingBranch.value = false;
            if (fullText) {
                const nodeInStore = storyNodes.value.find(n => n.id === nodeId);
                if (nodeInStore) nodeInStore.expanded_content = fullText;
            }
            return fullText;
        };

        // ── 执行副作用并刷新状态（共享工具函数）──
        // 返回 { fxContext, actionType } 供后续 expand 使用
        const applyBranchEffects = async (nodeId) => {
            try {
                const fxResp = await fetch(`${API_BASE_URL}/api/ai/apply-branch-effects`, {
                    method: 'POST',
                    headers: accountHeaders(true),
                    body: JSON.stringify({ node_id: nodeId, allow_ai_extraction: false })
                });
                const fxData = await fxResp.json();
                if (fxData.status === 'success') {
                    await Promise.all([fetchGameState(), fetchMapData()]);
                    const refreshed = storyNodes.value.find(n => n.id === nodeId);
                    if (refreshed) currentNode.value = refreshed;
                    if (fxData.spawned_npc) showNpcToast(fxData.spawned_npc);
                    if (fxData.stat_changes && fxData.stat_changes.length > 0) {
                        statChangesLog.value = fxData.stat_changes;
                        showStatChanges.value = true;
                        if (statChangesTimer) clearTimeout(statChangesTimer);
                        statChangesTimer = setTimeout(() => { showStatChanges.value = false; }, 10000);
                    }
                    if (fxData.bgm_name && applyBgmName(fxData.bgm_name)) {
                        await syncPlayerStateBgm({
                            bgm_url: currentTrackUrl.value || '',
                            bgm_name: currentTrackName.value || '',
                        });
                    }
                    return { fxContext: fxData.fx_context || '', actionType: fxData.action_type || '', bgmName: fxData.bgm_name || '' };
                }
            } catch (e) { console.error('apply-branch-effects failed:', e); }
            return { fxContext: '', actionType: '', bgmName: '' };
        };

        const jumpToNode = async (nodeId, optionText = '') => {
            const next=storyNodes.value.find(n=>n.id===nodeId); if(!next)return;
            const prevNode = currentNode.value; // 保存父场景引用
            const isNew=!prevNode||prevNode.id!==next.id;

            // 【时间回溯】：副作用执行前保存快照，失败静默忽略，绝不阻断正常推演
            if (optionText && isNew && prevNode && !isEditMode.value) {
                try {
                    await createGameCheckpoint('场景推进', prevNode.id);
                } catch(_) {}
            }

            currentNode.value=next; applyNodeSceneImage(next); syncEditData();
            optionLikelihoods.value = {}; // 跳转后清空概率（只对当前推演结果有效）
            if(!isNew||isEditMode.value)return;

            // 预设行动只执行已生成的确定性副作用；新叙事生成留给显式 AI-GM 操作。
            // 手动点击场景节点不触发任何推演副作用。
            try {
                if (optionText) {
                // 执行已生成的确定性副作用；点击预设行动不再自动调用聊天模型扩写。
                await applyBranchEffects(next.id);
                } // end if (optionText)

                // 只有通过选项正常推进时才同步地图位置
                // GM 从节点树手动跳转（optionText 为空）不同步，避免地图瞬移破坏叙事逻辑
                if (optionText) {
                    try {
                        const roomRes = await fetch(`${API_BASE_URL}/api/map/room-by-node/${next.id}`);
                        const roomData = await roomRes.json();
                        if (roomData.room_id) {
                            await fetch(`${API_BASE_URL}/api/map/move?room_id=${roomData.room_id}`, { method: 'POST', headers: accountHeaders() });
                            await fetchMapData();
                        }
                    } catch(_) {}
                }
            } catch(e) { console.error('branch expand/apply failed:', e); }

            // 只有通过预设选项跳转才记录到记忆流
            if(optionText) {
                fetch(`${API_BASE_URL}/api/game/log-scene-visit`,{method:'POST',headers:accountHeaders(true),body:JSON.stringify({node_id:next.id,node_name:next.name,option_text:optionText})}).catch(()=>{});
                fetch(`${API_BASE_URL}/api/game/check-triggers`,{method:'POST',headers:accountHeaders(true),body:JSON.stringify({scene_id:next.id,scene_name:next.name,scene_content:next.content||'',allow_ai:false})})
                .then(r=>{if(!r.ok)throw new Error(`HTTP ${r.status}`);return r.json();}).then(d=>{
                    console.log('[check-triggers]', d);
                    if(d.fired&&d.fired.length>0){
                        d.fired.forEach(fired=>{showTriggerAlert(fired);
                            if(fired.mode==='soft'&&currentNode.value&&currentNode.value.id===next.id){
                                if(!currentNode.value.options)currentNode.value.options=[];
                                if(!currentNode.value.options.some(o=>o.next_node_id===fired.target_node_id&&o._trigger))
                                    currentNode.value.options.push({id:'trigger_'+fired.trigger_id,text:'[触发机制] '+fired.target_node_name,next_node_id:fired.target_node_id,_trigger:true});
                            }
                        }); fetchTriggers();
                    }
                    // 文本注入：向当前节点叙事追加内容（立即反映，无需刷新）
                    if(d.text_injections&&d.text_injections.length>0){
                        d.text_injections.forEach(inj=>{
                            if(currentNode.value&&currentNode.value.id===inj.node_id){
                                const appended = '\n\n' + inj.text;
                                if(currentNode.value.expanded_content){ currentNode.value.expanded_content+=appended; expandingBranchText.value+=appended; }
                                else { currentNode.value.content+=appended; }
                            }
                        });
                    }
                    // 选项注入：向当前节点追加新选项
                    if(d.option_injections&&d.option_injections.length>0){
                        d.option_injections.forEach(inj=>{
                            if(currentNode.value&&currentNode.value.id===inj.node_id){
                                if(!currentNode.value.options)currentNode.value.options=[];
                                if(!currentNode.value.options.some(o=>o.id===inj.option_id))
                                    currentNode.value.options.push({id:inj.option_id,text:inj.text,next_node_id:inj.next_node_id,_injected:true});
                            }
                        });
                    }
                    // AI 即时生成节点：加入节点树并处理跳转
                    if(d.generated_nodes&&d.generated_nodes.length>0){
                        d.generated_nodes.forEach(gen=>{
                            storyNodes.value.push({id:gen.node_id,name:gen.node_name,summary:'',content:gen.node_content,expanded_content:'',options:[]});
                            const genLabel = '[AI生成] ' + gen.node_name;
                            showTriggerAlert({trigger_id:gen.trigger_id,label:'AI生成：'+gen.node_name,mode:gen.mode,target_node_id:gen.node_id,target_node_name:gen.node_name,fire_count:1,actions_log:gen.actions_log||[]});
                            if(gen.mode==='hard'){
                                triggerAlertTimer = setTimeout(()=>{ jumpToNode(gen.node_id, genLabel); triggerAlert.value=null; }, 3000);
                            } else if(gen.mode==='soft'&&currentNode.value&&currentNode.value.id===next.id){
                                if(!currentNode.value.options)currentNode.value.options=[];
                                currentNode.value.options.push({id:'gen_'+gen.node_id,text:genLabel,next_node_id:gen.node_id,_trigger:true,_generated:true});
                            }
                        });
                    }
                }).catch(e=>{ console.error('[check-triggers error]', e); });
            }
            await syncPlayerStateBgm({current_scene_id:next.id,scene_ai_text:''});
            await syncMultiplayerRoomState();
            if (optionText) {
                const sceneText = next.expanded_content || expandingBranchText.value || next.content || '';
                await postMultiplayerAiEvent(
                    'ai',
                    `场景推进：${next.name}\n${sceneText}`,
                    { source: 'scene_transition', node_id: next.id, option_text: optionText },
                );
            }
        };

        // ── Worldview / Memory / Lorebook ──
        const fetchWorldview=async()=>{try{const r=await fetch(`${API_BASE_URL}/api/game/worldview`);worldviewContent.value=(await r.json()).content;}catch(e){}};
        const saveWorldview=async()=>{try{await fetch(`${API_BASE_URL}/api/game/worldview`,{method:'PUT',headers:accountHeaders(true),body:JSON.stringify({content:worldviewContent.value})});showWorldviewModal.value=false;}catch(e){}};
        const openWorldviewModal=()=>{enterAdvancedPanel();fetchWorldview();showWorldviewModal.value=true;};
        const fetchMemory=async()=>{const r=await fetch(`${API_BASE_URL}/api/game/memory`);memoryContent.value=(await r.json()).content;};
        const saveMemory=async()=>{await fetch(`${API_BASE_URL}/api/game/memory`,{method:'PUT',headers:accountHeaders(true),body:JSON.stringify({content:memoryContent.value})});showMemoryModal.value=false;};
        const openMemoryModal=()=>{enterAdvancedPanel();fetchMemory();showMemoryModal.value=true;};
        const fetchLorebook=async()=>{const r=await fetch(`${API_BASE_URL}/api/game/lorebook`);lorebook.value=(await r.json()).lorebook;};
        const openLorebookModal=()=>{enterAdvancedPanel();fetchLorebook();showLorebookModal.value=true;currentLore.value={keywords:'',content:''};};
        const saveLore=async()=>{if(!currentLore.value.keywords||!currentLore.value.content)return;await fetch(`${API_BASE_URL}/api/game/lorebook`,{method:'POST',headers:accountHeaders(true),body:JSON.stringify({keywords:currentLore.value.keywords,content:currentLore.value.content})});currentLore.value={keywords:'',content:''};await fetchLorebook();};
        const deleteLore=async id=>{if(!confirm('确定移除该百科记录？'))return;await fetch(`${API_BASE_URL}/api/game/lorebook/${id}`,{method:'DELETE',headers:accountHeaders()});currentLore.value={keywords:'',content:''};await fetchLorebook();};

        // ── Node / Option CRUD ──
        const saveNodeChanges=async()=>{if(!currentNode.value)return;await fetch(`${API_BASE_URL}/api/game/node/${currentNode.value.id}`,{method:'PUT',headers:accountHeaders(true),body:JSON.stringify(editData.value)});await fetchGameState();};
        const createNewNode=async()=>{const r=await fetch(`${API_BASE_URL}/api/game/node`,{method:'POST',headers:accountHeaders(true),body:JSON.stringify({name:'未命名场景',summary:'剧情摘要…',content:'输入环境与剧情…'})});const d=await r.json();await fetchGameState();jumpToNode(d.id);};
        const deleteCurrentNode=async()=>{if(!confirm('危险操作：彻底删除此场景节点？'))return;await fetch(`${API_BASE_URL}/api/game/node/${currentNode.value.id}`,{method:'DELETE',headers:accountHeaders()});currentNode.value=null;await fetchGameState();};
        const addManualOption=async()=>{if(!newOptionText.value||!newOptionTarget.value)return;await fetch(`${API_BASE_URL}/api/game/option`,{method:'POST',headers:accountHeaders(true),body:JSON.stringify({node_id:currentNode.value.id,text:newOptionText.value,next_node_id:parseInt(newOptionTarget.value)})});newOptionText.value='';newOptionTarget.value='';await fetchGameState();};
        const deleteOption=async optId=>{await fetch(`${API_BASE_URL}/api/game/option/${optId}`,{method:'DELETE',headers:accountHeaders()});await fetchGameState();};

        // ── Characters ──
        const saveStatLabels=async()=>{try{await fetch(`${API_BASE_URL}/api/game/stat-labels`,{method:'PUT',headers:accountHeaders(true),body:JSON.stringify({hp_label:hpLabel.value,san_label:sanLabel.value})});}catch(e){}};
        const saveCharacterState=async char=>{try{await fetch(`${API_BASE_URL}/api/game/character/${char.id}`,{method:'POST',headers:accountHeaders(true),body:JSON.stringify({name:char.name,hp:char.hp,san:char.san,inventory:char.inventory||'',personality:char.personality||'',status:char.status||'active'})});await postMultiplayerAiEvent('state',`${char.name} 状态更新：${hpLabel.value} ${char.hp}，${sanLabel.value} ${char.san}，${{active:'在场',hidden:'未登场',benched:'暂离',dead:'死亡'}[char.status||'active']||char.status||'在场'}${char.inventory?'；'+char.inventory:''}`,{source:'character_update',character:{id:char.id,name:char.name,hp:char.hp,san:char.san,inventory:char.inventory||'',status:char.status||'active'}});}catch(e){}};
        const createCharacter=async()=>{if(!newChar.value.name.trim())return;try{await fetch(`${API_BASE_URL}/api/game/character`,{method:'POST',headers:accountHeaders(true),body:JSON.stringify(newChar.value)});showCharModal.value=false;resetNewChar();await fetchGameState();}catch(e){}};
        const deleteCharacter=async charId=>{if(!confirm('放逐该实体？'))return;try{await fetch(`${API_BASE_URL}/api/game/character/${charId}`,{method:'DELETE',headers:accountHeaders()});await fetchGameState();}catch(e){}};

        // ── NPC Persona 编辑 ──
        const openPersonaModal = (char) => {
            personaTarget.value = char;
            // 从独立的 personality 字段解析
            const p = char.personality || '';
            const mbtiMatch = p.match(/\[([A-Z]{4}[^\]]*)\]/);
            personaMbti.value = mbtiMatch ? mbtiMatch[1] : '';
            const quirkMatch = p.match(/\{([^}]+)\}/);
            personaQuirks.value = quirkMatch ? quirkMatch[1].split('/').map(s=>s.trim()).filter(Boolean) : [];
            const noteMatch = p.match(/<([^>]+)>/);
            personaNote.value = noteMatch ? noteMatch[1] : '';
            showPersonaModal.value = true;
        };

        const buildPersonaString = () => {
            const parts = [];
            if (personaMbti.value) parts.push(`[${personaMbti.value}]`);
            if (personaQuirks.value.length) parts.push(`{${personaQuirks.value.join(' / ')}}`);
            if (personaNote.value.trim()) parts.push(`<${personaNote.value.trim()}>`);
            return parts.join(' ');
        };

        const savePersona = async () => {
            if (!personaTarget.value) return;
            const char = personaTarget.value;
            char.personality = buildPersonaString();
            await saveCharacterState(char);
            showPersonaModal.value = false;
        };

        const clearPersona = () => {
            personaMbti.value = '';
            personaQuirks.value = [];
            personaNote.value = '';
        };

        // ── NPC 情绪 & 记忆读取（从 worldEntities 按名字查找） ──
        const getEntityEmotion = (charName) => {
            const entity = worldEntities.value.find(e => e.name === charName && e.entity_type === 'npc');
            if (!entity || !entity.state_desc) return null;
            const raw = entity.state_desc;
            if (typeof raw !== 'string' || !raw.trim().startsWith('{')) return null;
            try {
                const sd = JSON.parse(raw);
                const emo = sd.emotion;
                if (!emo || typeof emo !== 'object') return null;
                if (!('trust' in emo) && !('fear' in emo) && !('irritation' in emo)) return null;
                const bp = sd.breakpoint || {};
                const field = bp.trigger_field || 'irritation';
                const threshold = bp.threshold || 70;
                emo._nearBreak = (emo[field] || 0) >= threshold * 0.8;
                emo._broken = (emo[field] || 0) >= threshold && !!bp.reaction;
                return emo;
            } catch(e) { return null; }
        };

        const getEntityMemories = (charName) => {
            const entity = worldEntities.value.find(e => e.name === charName && e.entity_type === 'npc');
            if (!entity || !entity.state_desc) return [];
            const raw = entity.state_desc;
            if (!raw.trim().startsWith('{')) return [];
            try {
                return JSON.parse(raw).memory || [];
            } catch(e) { return []; }
        };

        // ── NPC 破防设定 (persona modal 扩展) ──
        const personaBreakpoint = ref({ threshold: 70, trigger_field: 'irritation', reaction: '' });
        const personaMemories = ref([]);
        const personaEmotion = ref({ trust: 0, fear: 0, irritation: 0 });

        // 升级 openPersonaModal：加载情绪/记忆/破防数据
        const _origOpenPersona = openPersonaModal;
        const openPersonaModalFull = async (char) => {
            _origOpenPersona(char);
            // 尝试从 world_entities 加载完整 persona
            const entity = worldEntities.value.find(e => e.name === char.name && e.entity_type === 'npc');
            if (entity) {
                try {
                    const r = await fetch(`${API_BASE_URL}/api/world-entities/${entity.id}/persona`);
                    const d = await r.json();
                    if (d.status === 'success' && d.persona) {
                        personaEmotion.value = d.persona.emotion || { trust: 0, fear: 0, irritation: 0 };
                        personaMemories.value = d.persona.memory || [];
                        personaBreakpoint.value = d.persona.breakpoint || { threshold: 70, trigger_field: 'irritation', reaction: '' };
                    }
                } catch(e) {
                    personaEmotion.value = { trust: 0, fear: 0, irritation: 0 };
                    personaMemories.value = [];
                    personaBreakpoint.value = { threshold: 70, trigger_field: 'irritation', reaction: '' };
                }
            } else {
                personaEmotion.value = { trust: 0, fear: 0, irritation: 0 };
                personaMemories.value = [];
                personaBreakpoint.value = { threshold: 70, trigger_field: 'irritation', reaction: '' };
            }
        };

        // 升级 savePersona：同时保存情绪/记忆/破防到 world_entities
        const savePersonaFull = async () => {
            if (!personaTarget.value) return;
            const char = personaTarget.value;
            char.personality = buildPersonaString();
            await saveCharacterState(char);
            // 保存情绪/记忆/破防到 world_entities
            const entity = worldEntities.value.find(e => e.name === char.name && e.entity_type === 'npc');
            if (entity) {
                await fetch(`${API_BASE_URL}/api/world-entities/${entity.id}/persona`, {
                    method: 'PUT', headers: accountHeaders(true),
                    body: JSON.stringify({
                        emotion: personaEmotion.value,
                        breakpoint: personaBreakpoint.value,
                        memory: personaMemories.value,
                    })
                }).catch(()=>{});
                await fetchWorldEntities();
            }
            showPersonaModal.value = false;
        };

        const removePersonaMemory = (idx) => {
            personaMemories.value = personaMemories.value.filter((_, i) => i !== idx);
        };
        const generateNPC=async()=>{if(!currentNode.value)return;isGeneratingNPC.value=true;try{const r=await fetch(`${API_BASE_URL}/api/ai/generate-npc`,{method:'POST',headers:accountHeaders(true),body:JSON.stringify({scene_name:currentNode.value.name,scene_content:currentNode.value.content,player_action:playerAction.value||'Explore'})});const d=await r.json();if(d.status==='success'){await fetchGameState();showNpcToast(d.npc);}}catch(e){}finally{isGeneratingNPC.value=false;}};

        // ── 时间回溯 ──
        const goBack = async () => {
            if (checkpointCount.value === 0 || isRollingBack.value) return;
            isRollingBack.value = true;
            try {
                const r = await fetch(`${API_BASE_URL}/api/game/rollback`, {method:'POST', headers:accountHeaders()});
                const d = await r.json();
                if (d.status === 'success') {
                    checkpointCount.value = d.remaining ?? Math.max(0, checkpointCount.value - 1);
                    await Promise.all([fetchGameState(), fetchMapData(), fetchPlayerStateBgm()]);
                    const node = storyNodes.value.find(n => n.id === d.restored_node_id);
                    if (node) { currentNode.value = node; applyNodeSceneImage(node); syncEditData(); }
                    await persistSoloSessionProgress();
                }
            } catch(_) {}
            finally { isRollingBack.value = false; }
        };

        // ── AI ──
        const generateAIText = async () => {
            if (!currentNode.value) return;
            const _nodeId = currentNode.value.id;
            isGeneratingText.value = true;
            try {
                const r = await fetch(`${API_BASE_URL}/api/ai/expand-text`, {
                    method: 'POST',
                    headers: accountHeaders(true),
                    body: JSON.stringify({scene_name: currentNode.value.name || '', content: currentNode.value.content || ''})
                });
                const data = await r.json();
                if (data.generated_text) {
                    aiGeneratedText.value = {...aiGeneratedText.value, [_nodeId]: data.generated_text};
                    await syncPlayerStateBgm({
                        current_scene_id: _nodeId,
                        scene_ai_text: data.generated_text || '',
                    });
                }
            } catch(e) {
            } finally {
                isGeneratingText.value = false;
            }
        };
        const generateDynamicOptions=async(correctionText='', actionOverride='')=>{
            if(!currentNode.value||((!playerAction.value)&&!actionOverride&&!correctionText&&!lastDynamicContext.value))return null;
            isGeneratingOptions.value=true;
            // 确定本次推演的行动文本
            const action = actionOverride || (correctionText ? (lastDynamicContext.value?.action || playerAction.value) : playerAction.value);
            const sceneName = currentNode.value.name;
            const content = currentNode.value.content;
            const nodeId = currentNode.value.id;
            try{
                const body = {
                    current_node_id: nodeId,
                    scene_name: sceneName || '',
                    content: content || '',
                    player_action: action || '',
                    action_type: actionType.value || 'mixed',
                };
                if(correctionText) body.gm_correction = correctionText;
                if(narrativeMood.value) body.mood = narrativeMood.value;
                if(forceNarrativeThrust.value) body.force_thrust = true;

                const r=await fetch(`${API_BASE_URL}/api/ai/dynamic-options`,{
                    method:'POST',headers:accountHeaders(true),body:JSON.stringify(body)
                });
                const d=await r.json();
                if(d.status==='success'){
                    if(!correctionText) playerAction.value='';
                    gmCorrection.value='';
                    forceNarrativeThrust.value = false;
                    // 提取分支概率
                    const newLikelihoods = {};
                    (d.new_options||[]).forEach(opt => { if(opt.likelihood) newLikelihoods[opt.next_node_id] = opt.likelihood; });
                    optionLikelihoods.value = newLikelihoods;
                    // 保存推演上下文供 GM 干预重试 + 脑内剧场展示
                    lastDynamicContext.value = {
                        action: action,
                        sceneName: sceneName,
                        content: content,
                        nodeId: nodeId,
                        branchCount: (d.new_options||[]).length,
                        thoughtProcess: d.thought_process || '',
                    };
                    await fetchGameState();
                    if(d.spawned_npc)showNpcToast(d.spawned_npc);
                    if(d.stat_changes&&d.stat_changes.length>0){
                        statChangesLog.value=d.stat_changes;showStatChanges.value=true;
                        if(statChangesTimer)clearTimeout(statChangesTimer);
                        statChangesTimer=setTimeout(()=>{showStatChanges.value=false;},10000);
                    }
                    return d;
                } else {
                    alert(d.message || '推演失败');
                    return null;
                }
            }catch(e){console.error(e);return null;}finally{isGeneratingOptions.value=false;}
        };
        const submitSoloAction = async () => {
            const raw = playerAction.value.trim();
            if (!raw || !soloHasConfirmedCharacters.value || isGeneratingOptions.value) return;
            const actor = soloActorLabel.value;
            const actionForAi = `${actor}：${raw}`;
            addSoloLog('player', actor, raw);
            await createGameCheckpoint('自由行动', currentNode.value?.id);
            const result = await generateDynamicOptions('', actionForAi);
            if (result?.status === 'success') {
                addSoloLog('ai', 'AI-GM', summarizeAiOptionsForSolo());
                await persistSoloSessionProgress();
            }
            else playerAction.value = raw;
        };
        // GM 干预：用纠正指令重新推演
        const retryWithCorrection=()=>{
            if(!gmCorrection.value.trim()||isGeneratingOptions.value)return;
            generateDynamicOptions(gmCorrection.value.trim());
        };
        const publishGmManualEvent = async () => {
            const text = gmManualEventText.value.trim();
            if (!text || gmManualEventBusy.value) return;
            gmManualEventBusy.value = true;
            gmManualEventMsg.value = '';
            try {
                const payload = {
                    content: text,
                    kind: gmManualEventKind.value || 'ai',
                    sender_name: 'GM',
                    current_scene_id: currentNode.value?.id || 0,
                    sync_player_state: gmManualSyncPlayer.value,
                    record_to_memory: gmManualRecordMemory.value,
                };
                const response = await fetch(`${API_BASE_URL}/api/game/gm-event`, {
                    method: 'POST',
                    headers: accountHeaders(true),
                    body: JSON.stringify(payload),
                });
                const data = await response.json().catch(() => ({}));
                if (!response.ok || data.status === 'error') throw new Error(data.detail || data.message || '发布失败');
                if (currentNode.value) {
                    aiGeneratedText.value = { ...aiGeneratedText.value, [currentNode.value.id]: text };
                }
                addSoloLog('ai', 'GM', text);
                await postMultiplayerAiEvent(gmManualEventKind.value || 'ai', text, {
                    source: 'gm_manual_control',
                    node_id: currentNode.value?.id || null,
                }, { senderName: 'GM' });
                gmManualEventText.value = '';
                gmManualEventMsg.value = '已发布到当前玩家视图';
                setTimeout(() => { gmManualEventMsg.value = ''; }, 2500);
                await persistSoloSessionProgress();
            } catch (err) {
                gmManualEventMsg.value = err?.message || '发布失败';
            } finally {
                gmManualEventBusy.value = false;
            }
        };
        const openBattleReportModal=()=>{showBattleReportModal.value=true;battleReport.value='';battleReportFilename.value='';};
        const generateBattleReport=async()=>{isGeneratingReport.value=true;try{const r=await fetch(`${API_BASE_URL}/api/ai/export-battle-report`,{method:'POST',headers:accountHeaders()});const d=await r.json();if(d.status==='success'){battleReport.value=d.report;battleReportFilename.value=d.filename;}}catch(e){}finally{isGeneratingReport.value=false;}};
        const downloadBattleReport=()=>{if(!battleReport.value)return;const b=new Blob([battleReport.value],{type:'text/markdown;charset=utf-8'});const u=URL.createObjectURL(b);const a=document.createElement('a');a.href=u;a.download=battleReportFilename.value||'battle_report.md';a.click();URL.revokeObjectURL(u);};

        // ── Images ──
        const generateImage = async () => {
            if (!currentNode.value) return;
            isGeneratingImage.value = true;
            isLoadingImg.value = true;
            imgLoadError.value = false;
            generatedImageUrl.value = '';
            const _aiText = aiGeneratedText.value[currentNode.value.id] || '';
            const _sceneText = currentNode.value.expanded_content || currentNode.value.content || '';
            const desc = imgPrompt.value.trim() || (currentNode.value.name + ' ' + _sceneText + (_aiText ? '\n' + _aiText : ''));
            try {
                const r = await fetch(`${API_BASE_URL}/api/ai/generate-image`, {
                    method: 'POST',
                    headers: accountHeaders(true),
                    body: JSON.stringify({ description: desc, style: imgStyle.value, image_model: imgModel.value })
                });
                const d = await r.json();
                if (d.status === 'success') {
                    imgEnPrompt.value = d.en_prompt || '';
                    imgPromptUsed.value = d.prompt_used || '';
                    const preload = new window.Image();
                    preload.onload = async () => {
                        generatedImageUrl.value = d.image_url;
                        isLoadingImg.value = false;
                        if (currentNode.value) {
                            await syncPlayerStateBgm({
                                current_scene_id: currentNode.value.id,
                                scene_image: d.image_url,
                                scene_prompt: d.prompt_used || '',
                            });
                        }
                    };
                    preload.onerror = () => { imgLoadError.value = true; isLoadingImg.value = false; };
                    preload.src = d.image_url;
                } else {
                    isLoadingImg.value = false;
                    imgLoadError.value = true;
                }
            } catch (e) {
                isLoadingImg.value = false;
                imgLoadError.value = true;
            } finally {
                isGeneratingImage.value = false;
            }
        };

        const togglePlay = async () => {
            if(!audioRef.value) return;
            if(isPlaying.value) {
                audioRef.value.pause();
                isPlaying.value = false;
            } else {
                await playCurrentTrack();
            }
        };

        const changeTrack = async () => {
            if (isPlaying.value) await playCurrentTrack();
            await syncPlayerStateBgm();
        };

        // ── 叙事控制台 ──
        const doTimeSkip = async () => {
            if (!timeSkipInput.value.trim() || isGeneratingOptions.value) return;
            playerAction.value = `【时间快进：${timeSkipInput.value.trim()}】`;
            timeSkipInput.value = '';
            await generateDynamicOptions();
        };

        const doForceThrust = async () => {
            if (isGeneratingOptions.value || !currentNode.value) return;
            forceNarrativeThrust.value = true;
            await generateDynamicOptions();
        };

        const requestDiceService = async (path, body) => {
            const response = await fetch(`${API_BASE_URL}${path}`, {
                method: 'POST',
                headers: accountHeaders(true),
                body: JSON.stringify(body)
            });
            let data = null;
            try { data = await response.json(); } catch(e) {}
            if (!response.ok || data?.status === 'error') {
                throw new Error(data?.detail || data?.message || `${response.status} ${response.statusText}`);
            }
            return data;
        };

        const diceNumericValue = (fallback = 60) => {
            const raw = dicePanel.value.skill_value;
            if (raw === '' || raw === null || raw === undefined) return fallback;
            const value = Number(raw);
            return Number.isFinite(value) ? value : fallback;
        };

        const rollGmDice = async (mode = 'roll') => {
            if (diceBusy.value) return;
            diceBusy.value = true;
            diceError.value = '';
            try {
                const actorName = dicePanel.value.actor_name?.trim() || 'GM';
                const reason = dicePanel.value.reason?.trim() || '';
                const record = !!dicePanel.value.record_to_memory;
                let result;
                if (mode === 'coc') {
                    result = await requestDiceService('/api/dice/coc/check', {
                        actor_name: actorName,
                        skill_name: dicePanel.value.skill_name?.trim() || '侦查',
                        skill_value: diceNumericValue(60),
                        reason,
                        group_id: 'main',
                        record_to_memory: record
                    });
                } else if (mode === 'san') {
                    const expression = dicePanel.value.expression?.trim() || '';
                    result = await requestDiceService('/api/dice/coc/san', {
                        actor_name: actorName,
                        san: diceNumericValue(60),
                        loss_formula: expression.includes('/') ? expression : '1/1d6',
                        record_to_memory: record
                    });
                } else {
                    const payload = {
                        expression: dicePanel.value.expression?.trim() || '1d100',
                        actor_name: actorName,
                        reason,
                        group_id: 'main',
                        record_to_memory: record
                    };
                    if (dicePanel.value.skill_name?.trim()) payload.skill_name = dicePanel.value.skill_name.trim();
                    const rawValue = dicePanel.value.skill_value;
                    if (rawValue !== '' && rawValue !== null && rawValue !== undefined) payload.skill_value = diceNumericValue(60);
                    result = await requestDiceService('/api/dice/roll', payload);
                }
                diceResult.value = result;
                await postMultiplayerAiEvent('dice', diceResultText.value, {
                    source: 'gm_dice',
                    mode,
                    result,
                });
            } catch (err) {
                diceError.value = err?.message || '骰子服务不可用';
            } finally {
                diceBusy.value = false;
            }
        };

        const clearDiceResult = () => {
            diceError.value = '';
            diceResult.value = null;
        };

        const fatePick = () => {
            const lh = optionLikelihoods.value;
            const opts = currentNode.value?.options;
            if (!opts?.length || !Object.keys(lh).length || fateSpinning.value) return;

            // 加权随机算出赢家
            // 只对有概率映射的选项参与抉择（排除原有选项）
            const eligibleOpts = opts.filter(o => lh[o.next_node_id]);
            if (!eligibleOpts.length) return;
            const weightMap = { '极高': 50, '高': 35, '中等': 20, '低': 10, '极低': 3 };
            const weights = eligibleOpts.map(o => {
                const val = lh[o.next_node_id] || '';
                for (const [key, w] of Object.entries(weightMap)) {
                    if (val.startsWith(key)) return w;
                }
                return 20;
            });
            const total = weights.reduce((a, b) => a + b, 0);
            let rand = Math.random() * total;
            let winnerIdx = eligibleOpts.length - 1;
            for (let i = 0; i < eligibleOpts.length; i++) {
                rand -= weights[i];
                if (rand <= 0) { winnerIdx = i; break; }
            }

            // 命运转轮动画：在 eligibleOpts 中循环高亮（用 next_node_id 对应原始 opts 的位置）
            // 转轮高亮基于 opts 的真实下标，需要找到 eligibleOpts[i] 在 opts 中的位置
            const eligibleIndices = eligibleOpts.map(o => opts.findIndex(x => x.next_node_id === o.next_node_id));
            fateSpinning.value = true;
            const n = eligibleOpts.length;
            const totalTicks = 16 + Math.floor(Math.random() * 7); // 16-22 帧
            // 反推起始位置（在 eligibleIndices 数组中的偏移），使顺序循环恰好以 winnerIdx 结尾
            const startOffset = (((winnerIdx - (totalTicks - 1) % n) % n) + n) % n;
            let delay = 0;
            for (let step = 0; step < totalTicks; step++) {
                const progress = step / (totalTicks - 1);
                const interval = 55 + Math.pow(progress, 1.8) * 305;
                delay += interval;
                const eligiblePos = (startOffset + step) % n;
                const realIdx = eligibleIndices[eligiblePos]; // opts 中的真实下标
                setTimeout(() => { fateHighlightIdx.value = realIdx; }, delay);
            }
            // 停顿后跳转
            setTimeout(() => {
                fateSpinning.value = false;
                fateHighlightIdx.value = -1;
                selectSoloVisibleOption(eligibleOpts[winnerIdx]);
            }, delay + 650);
        };

        // ── Tree Layout ──
        const treeLayout = computed(() => {
            const nodes = storyNodes.value;
            if (!nodes || nodes.length === 0) return { nodes: [], edges: [], width: 300, height: 200 };

            const allOpts = nodes.flatMap(n => (n.options || []).map(o => ({ from: n.id, to: o.next_node_id })));
            const toIds = new Set(allOpts.map(e => e.to));
            const roots = nodes.filter(n => !toIds.has(n.id));
            const startId = roots.length > 0 ? roots[0].id : nodes[0].id;

            const levelMap = {};
            const visited = new Set();
            const queue = [[startId, 0]];
            while (queue.length > 0) {
                const [id, lvl] = queue.shift();
                if (visited.has(id)) continue;
                visited.add(id);
                levelMap[id] = lvl;
                allOpts.filter(e => e.from === id).map(e => e.to).forEach(cid => {
                    if (!visited.has(cid)) queue.push([cid, lvl + 1]);
                });
            }

            nodes.forEach(n => { if (!(n.id in levelMap)) levelMap[n.id] = 0; });
            const levels = {};
            Object.entries(levelMap).forEach(([id, lvl]) => {
                if (!levels[lvl]) levels[lvl] = [];
                levels[lvl].push(parseInt(id));
            });

            const COL_W = 150, ROW_H = 80, PAD = 40;
            const maxCols = Math.max(...Object.values(levels).map(a => a.length));
            const maxRows = Object.keys(levels).length;

            const posMap = {};
            Object.entries(levels).forEach(([lvl, ids]) => {
                ids.forEach((id, i) => {
                    posMap[id] = { x: PAD + i * COL_W + COL_W / 2, y: PAD + parseInt(lvl) * ROW_H + ROW_H / 2 };
                });
            });

            const layoutNodes = nodes.map(n => ({ id: n.id, name: n.name, x: posMap[n.id]?.x ?? PAD, y: posMap[n.id]?.y ?? PAD }));
            const edgeSet = new Set();
            const edges = [];
            allOpts.forEach(e => {
                const key = `${e.from}-${e.to}`;
                if (!edgeSet.has(key) && posMap[e.from] && posMap[e.to]) {
                    edgeSet.add(key);
                    edges.push({ key, x1: posMap[e.from].x, y1: posMap[e.from].y + 20, x2: posMap[e.to].x, y2: posMap[e.to].y - 20 });
                }
            });
            return { nodes: layoutNodes, edges, width: Math.max(maxCols * COL_W + PAD * 2, 280), height: maxRows * ROW_H + PAD * 2 };
        });

        watch(isEditMode, v => { if (v) syncEditData(); });
        watch(soloPlayerName, saveSoloProfile);
        watch(currentNode, () => {
            if (activeSoloSessionId.value && playSurface.value === 'player' && appState.value === 'game') {
                updateSoloSessionRecord(activeSoloSessionId.value);
            }
        });
        watch(volume, v => { if (audioRef.value) audioRef.value.volume = v; });
        onMounted(async () => {
            await refreshMultiplayerAuthAccount();
            await fetchApiKeyStatus({ resetModels: true });
            await fetchCampaigns();
            await fetchSaves();
            await autoRestoreSoloSession();
            if (audioRef.value) audioRef.value.volume = 0.3;
        });

        // ══════════════════════════════════════════════════════
        // 【地图编辑器】：响应式状态与方法
        // ══════════════════════════════════════════════════════
        const MAP_GRID     = 40;
        const MAP_ID       = 1;   // 当前只支持单地图

        const showMapModal    = ref(false);
        const mapRooms        = ref([]);
        const mapEdges        = ref([]);
        const mapFloors       = ref([1]);          // 所有楼层列表
        const mapActiveFloor  = ref(1);            // 当前显示楼层
        const mapSelectedRoom      = ref(null);
        const mapSelectedEdge      = ref(null);
        const mapSelectedEntityDot = ref(null);   // 点击实体圆点时选中
        const mapActiveTool   = ref('select');
        const mapSnapGrid     = ref(true);
        const mapCanvasRef    = ref(null);
        const mapNameInputRef = ref(null);

        // 当前楼层可见的房间和边（computed）
        const mapVisibleRooms = computed(() =>
            mapRooms.value.filter(r => (r.floor ?? 1) === mapActiveFloor.value)
        );
        // 跨楼层连接线（楼梯/电梯/传送门）始终显示，仅当两端有一端在当前楼层时展示
        const mapVisibleEdges = computed(() => {
            const roomIds = new Set(mapVisibleRooms.value.map(r => r.id));
            return mapEdges.value.filter(e => {
                const fr = mapRooms.value.find(r => r.id === e.from_id);
                const tr = mapRooms.value.find(r => r.id === e.to_id);
                if (!fr || !tr) return false;
                const fromFloor = fr.floor ?? 1;
                const toFloor   = tr.floor ?? 1;
                // 同楼层：双端都在当前楼层
                if (fromFloor === toFloor) return fromFloor === mapActiveFloor.value;
                // 跨楼层：至少一端在当前楼层，显示为楼梯符号
                return fromFloor === mapActiveFloor.value || toFloor === mapActiveFloor.value;
            });
        });

        // 视口变换
        const mapScale  = ref(1);
        const mapViewX  = ref(0);
        const mapViewY  = ref(0);
        const mapSvgW   = ref(1000);
        const mapSvgH   = ref(700);

        // 鼠标 SVG 坐标
        const mapMouseSvg = ref({ x: 0, y: 0 });

        // 画房间
        const mapDrawingRoom  = ref(false);
        const mapDrawStart    = ref({ x: 0, y: 0 });

        // 命名浮层
        const mapNamingRoom   = ref(false);
        const mapNamingLabel  = ref('');
        const mapNamingPos    = ref({ x: 0, y: 0 });
        const mapPendingRect  = ref(null);   // { x, y, w, h }

        // 连接工具
        const mapConnectSource = ref(null);
        const mapDrawingEdge   = ref(false);

        // 拖动
        let mapDragRoom = null;
        let mapDragOffset = { x: 0, y: 0 };
        let mapPanning = false;
        let mapPanStart = { x: 0, y: 0, vx: 0, vy: 0 };

        const mapTools = [
            { id: 'select',  icon: 'ph-cursor',          label: '选择/拖动' },
            { id: 'room',    icon: 'ph-square',           label: '画房间'   },
            { id: 'connect', icon: 'ph-arrows-left-right',label: '连接通道' },
            { id: 'delete',  icon: 'ph-trash',            label: '删除'     },
        ];
        const mapRoomStates = [
            { v: 'unknown',  label: '未探索', color: '#4b5563', rgb: '107,114,128' },
            { v: 'explored', label: '已探索', color: '#4fc98a', rgb: '79,201,138'  },
            { v: 'locked',   label: '上锁',   color: '#e05c5c', rgb: '224,92,92'   },
            { v: 'active',   label: '当前位置',color: '#34d399', rgb: '52,211,153' },
        ];
        const mapRoomColors = [
            '#1e3a2f','#1a2e40','#2d1f3e','#3a1e1e','#2a2a1a',
            '#0f2a1e','#1a1a2e','#2e1a2e','#2e2010','#1a2a2a',
        ];

        // 工具函数
        const mapRoomById = (id) => mapRooms.value.find(r => r.id === id);
        watch(() => currentNode.value?.id, () => { syncMultiplayerRoomState(); });

        const mapClientToSvg = (e) => {
            const el = mapCanvasRef.value;
            if (!el) return { x: 0, y: 0 };
            const rect = el.getBoundingClientRect();
            const cx = (e.clientX - rect.left) / mapScale.value + mapViewX.value;
            const cy = (e.clientY - rect.top)  / mapScale.value + mapViewY.value;
            return { x: cx, y: cy };
        };

        const mapSnap = (v) => mapSnapGrid.value ? Math.round(v / MAP_GRID) * MAP_GRID : v;

        const closeAdvancedSystemPanels = () => {
            showWorldviewModal.value = false;
            showLorebookModal.value = false;
            showRagModal.value = false;
            showMapModal.value = false;
            showWorldEntitiesModal.value = false;
            showTriggerModal.value = false;
            showMemoryModal.value = false;
            timelinePanelOpen.value = false;
            showTlEditModal.value = false;
            showTlMemoryModal.value = false;
            showTlDynamicModal.value = false;
            showMergeModal.value = false;
            showTlRoomModal.value = false;
        };

        const enterAdvancedPanel = () => {
            closeAdvancedSystemPanels();
            showGameSettingsModal.value = false;
            showAdvancedSettings.value = true;
        };

        const returnToAdvancedSettings = () => {
            closeAdvancedSystemPanels();
            showAdvancedSettings.value = true;
            showGameSettingsModal.value = true;
        };

        // 数据获取
        const fetchMapData = async () => {
            try {
                const r = await fetch(`${API_BASE_URL}/api/map/rooms?map_id=${MAP_ID}`);
                const d = await r.json();
                mapRooms.value = (d.rooms || []).map(r => ({ ...r, floor: r.floor ?? 1 }));
                mapEdges.value = (d.edges || []).map(e => ({ ...e, edge_type: e.edge_type ?? 'normal' }));
                mapFloors.value = d.floors || [1];
                // 若当前楼层不在列表里，重置到第1层
                if (!mapFloors.value.includes(mapActiveFloor.value))
                    mapActiveFloor.value = mapFloors.value[0] || 1;
            } catch(e) {}
        };

        const openMapModal = () => {
            enterAdvancedPanel();
            fetchMapData();
            showMapModal.value = true;
            nextTick(() => {
                if (mapCanvasRef.value) {
                    mapSvgW.value = mapCanvasRef.value.clientWidth;
                    mapSvgH.value = mapCanvasRef.value.clientHeight;
                }
            });
        };

        // ── 鼠标事件处理 ──────────────────────────────────────

        const mapOnWheel = (e) => {
            const delta = e.deltaY > 0 ? 0.9 : 1.1;
            mapScale.value = Math.min(3, Math.max(0.3, mapScale.value * delta));
        };

        const mapOnCanvasMousedown = (e) => {
            const svg = mapClientToSvg(e);
            mapMouseSvg.value = svg;

            if (e.button === 1) {  // 中键平移
                mapPanning = true;
                mapPanStart = { x: e.clientX, y: e.clientY, vx: mapViewX.value, vy: mapViewY.value };
                return;
            }

            if (mapActiveTool.value === 'room') {
                mapDrawingRoom.value = true;
                mapDrawStart.value = { x: mapSnap(svg.x), y: mapSnap(svg.y) };
            }
            if (mapActiveTool.value === 'connect') {
                // 若点到空白处，取消连接
                const hit = mapRooms.value.find(r =>
                    svg.x >= r.x && svg.x <= r.x + r.w &&
                    svg.y >= r.y && svg.y <= r.y + r.h
                );
                if (!hit) { mapConnectSource.value = null; mapDrawingEdge.value = false; }
            }
        };

        const mapOnCanvasMousemove = (e) => {
            const svg = mapClientToSvg(e);
            mapMouseSvg.value = svg;

            if (mapPanning) {
                mapViewX.value = mapPanStart.vx - (e.clientX - mapPanStart.x) / mapScale.value;
                mapViewY.value = mapPanStart.vy - (e.clientY - mapPanStart.y) / mapScale.value;
                return;
            }

            if (mapDragRoom && mapActiveTool.value === 'select') {
                mapDragRoom.x = mapSnap(svg.x - mapDragOffset.x);
                mapDragRoom.y = mapSnap(svg.y - mapDragOffset.y);
            }
        };

        const mapOnCanvasMouseup = async (e) => {
            mapPanning = false;
            const svg = mapClientToSvg(e);

            // 完成画房间
            if (mapActiveTool.value === 'room' && mapDrawingRoom.value) {
                mapDrawingRoom.value = false;
                const x = mapSnap(Math.min(mapDrawStart.value.x, svg.x));
                const y = mapSnap(Math.min(mapDrawStart.value.y, svg.y));
                const w = Math.max(MAP_GRID * 2, mapSnap(Math.abs(svg.x - mapDrawStart.value.x)));
                const h = Math.max(MAP_GRID,     mapSnap(Math.abs(svg.y - mapDrawStart.value.y)));
                if (w > MAP_GRID && h > MAP_GRID * 0.8) {
                    mapPendingRect.value = { x, y, w, h };
                    mapNamingLabel.value = '';
                    // 浮层位置（屏幕坐标）
                    const el = mapCanvasRef.value.getBoundingClientRect();
                    mapNamingPos.value = {
                        x: Math.min((x - mapViewX.value) * mapScale.value + 10, el.width - 240),
                        y: Math.min((y - mapViewY.value) * mapScale.value + 10, el.height - 120)
                    };
                    mapNamingRoom.value = true;
                    nextTick(() => mapNameInputRef.value?.focus());
                }
            }

            // 完成拖动房间
            if (mapDragRoom && mapActiveTool.value === 'select') {
                await fetch(`${API_BASE_URL}/api/map/rooms/${mapDragRoom.id}`, {
                    method: 'PUT', headers: accountHeaders(true),
                    body: JSON.stringify({ ...mapDragRoom })
                }).catch(() => {});
                mapDragRoom = null;
            }
        };

        // ── 房间交互 ──────────────────────────────────────────

        const mapOnRoomMousedown = (e, room) => {
            if (mapActiveTool.value === 'select') {
                mapDragRoom = room;
                const svg = mapClientToSvg(e);
                mapDragOffset = { x: svg.x - room.x, y: svg.y - room.y };
            }
        };

        const mapOnRoomClick = async (room) => {
            if (mapActiveTool.value === 'delete') {
                if (!confirm(`删除房间「${room.label}」及其所有连接？`)) return;
                await fetch(`${API_BASE_URL}/api/map/rooms/${room.id}`, { method: 'DELETE', headers: accountHeaders() });
                await fetchMapData();
                if (mapSelectedRoom.value?.id === room.id) mapSelectedRoom.value = null;
                return;
            }
            if (mapActiveTool.value === 'connect') {
                if (!mapConnectSource.value) {
                    mapConnectSource.value = room.id;
                    mapDrawingEdge.value = true;
                } else if (mapConnectSource.value !== room.id) {
                    // 创建连接
                    await fetch(`${API_BASE_URL}/api/map/edges`, {
                        method: 'POST', headers: accountHeaders(true),
                        body: JSON.stringify({ map_id: MAP_ID, from_id: mapConnectSource.value, to_id: room.id })
                    });
                    mapConnectSource.value = null;
                    mapDrawingEdge.value = false;
                    await fetchMapData();
                }
                return;
            }
            if (mapActiveTool.value === 'select') {
                mapSelectedRoom.value = { ...room };
                mapSelectedEdge.value = null;
            }
        };

        const mapSelectEdge = (edge) => {
            if (mapActiveTool.value === 'delete') {
                fetch(`${API_BASE_URL}/api/map/edges/${edge.id}`, { method: 'DELETE', headers: accountHeaders() })
                    .then(() => fetchMapData());
                return;
            }
            mapSelectedEdge.value = { ...edge };
            mapSelectedRoom.value = null;
        };

        // 命名浮层确认/取消
        const mapConfirmRoom = async () => {
            if (!mapNamingLabel.value.trim() || !mapPendingRect.value) {
                mapCancelRoom(); return;
            }
            const r = await fetch(`${API_BASE_URL}/api/map/rooms`, {
                method: 'POST', headers: accountHeaders(true),
                body: JSON.stringify({
                    map_id: MAP_ID,
                    label: mapNamingLabel.value.trim(),
                    floor: mapActiveFloor.value,
                    ...mapPendingRect.value
                })
            });
            const d = await r.json();
            mapNamingRoom.value = false;
            mapPendingRect.value = null;
            await fetchMapData();
            // 自动选中刚创建的房间
            mapSelectedRoom.value = { ...mapRooms.value.find(r => r.id === d.id) };
        };

        const mapCancelRoom = () => {
            mapNamingRoom.value = false;
            mapPendingRect.value = null;
        };

        // ── 属性面板操作 ──────────────────────────────────────

        const mapSaveRoom = async () => {
            if (!mapSelectedRoom.value) return;
            // 同步到本地列表
            const idx = mapRooms.value.findIndex(r => r.id === mapSelectedRoom.value.id);
            if (idx >= 0) mapRooms.value[idx] = { ...mapSelectedRoom.value };
            await fetch(`${API_BASE_URL}/api/map/rooms/${mapSelectedRoom.value.id}`, {
                method: 'PUT', headers: accountHeaders(true),
                body: JSON.stringify(mapSelectedRoom.value)
            }).catch(() => {});
        };

        const mapSaveEdge = async () => {
            if (!mapSelectedEdge.value) return;
            const idx = mapEdges.value.findIndex(e => e.id === mapSelectedEdge.value.id);
            if (idx >= 0) mapEdges.value[idx] = { ...mapSelectedEdge.value };
            await fetch(`${API_BASE_URL}/api/map/edges/${mapSelectedEdge.value.id}`, {
                method: 'PUT', headers: accountHeaders(true),
                body: JSON.stringify(mapSelectedEdge.value)
            }).catch(() => {});
        };

        const mapSetRoomState = async (state) => {
            if (!mapSelectedRoom.value) return;
            mapSelectedRoom.value.state = state;
            await mapSaveRoom();
        };

        const mapSetActive = async (roomId) => {
            await fetch(`${API_BASE_URL}/api/map/move?room_id=${roomId}`, { method: 'POST', headers: accountHeaders() });
            mapRooms.value.forEach(r => {
                if (r.state === 'active') r.state = 'explored';
            });
            const r = mapRooms.value.find(r => r.id === roomId);
            if (r) r.state = 'active';
            if (mapSelectedRoom.value?.id === roomId) mapSelectedRoom.value.state = 'active';
            await syncMultiplayerRoomState();
        };

        const mapDeleteSelectedRoom = async () => {
            if (!mapSelectedRoom.value) return;
            if (!confirm(`删除房间「${mapSelectedRoom.value.label}」？`)) return;
            await fetch(`${API_BASE_URL}/api/map/rooms/${mapSelectedRoom.value.id}`, { method: 'DELETE', headers: accountHeaders() });
            mapSelectedRoom.value = null;
            await fetchMapData();
        };

        const mapDeleteSelectedEdge = async () => {
            if (!mapSelectedEdge.value) return;
            await fetch(`${API_BASE_URL}/api/map/edges/${mapSelectedEdge.value.id}`, { method: 'DELETE', headers: accountHeaders() });
            mapSelectedEdge.value = null;
            await fetchMapData();
        };

        const mapClearAll = async () => {
            if (!confirm('清空整张地图？此操作不可撤销。')) return;
            for (const r of mapRooms.value)
                await fetch(`${API_BASE_URL}/api/map/rooms/${r.id}`, { method: 'DELETE', headers: accountHeaders() }).catch(() => {});
            mapRooms.value = [];
            mapEdges.value = [];
            mapFloors.value = [1];
            mapActiveFloor.value = 1;
            mapSelectedRoom.value = null;
            mapSelectedEdge.value = null;
        };

        // ── 楼层管理 ──────────────────────────────────────────
        const mapAddFloor = (direction = 1) => {
            const current = mapFloors.value;
            const newFloor = direction > 0
                ? Math.max(...current) + 1          // 向上：B2F → B3F 或 1F → 2F
                : Math.min(...current) - 1;         // 向下：1F → B1（0）→ B2（-1）
            mapFloors.value = [...current, newFloor].sort((a, b) => b - a);  // 高层在前
            mapActiveFloor.value = newFloor;
        };

        const mapRemoveFloor = async (floor) => {
            const roomsOnFloor = mapRooms.value.filter(r => (r.floor ?? 1) === floor);
            if (roomsOnFloor.length > 0) {
                if (!confirm(`第 ${floor} 层有 ${roomsOnFloor.length} 个房间，确认连同房间一起删除？`)) return;
                for (const r of roomsOnFloor)
                    await fetch(`${API_BASE_URL}/api/map/rooms/${r.id}`, { method: 'DELETE', headers: accountHeaders() }).catch(() => {});
            }
            await fetchMapData();
            if (mapActiveFloor.value === floor)
                mapActiveFloor.value = mapFloors.value[0] || 1;
        };

        // 将选中房间移到指定楼层
        const mapMoveRoomToFloor = async (floor) => {
            if (!mapSelectedRoom.value) return;
            mapSelectedRoom.value.floor = floor;
            await mapSaveRoom();
            await fetchMapData();
        };

        // 创建跨楼层连接（楼梯/电梯等）
        const mapConnectCrossFloor = async (fromRoomId, toRoomId, edgeType) => {
            await fetch(`${API_BASE_URL}/api/map/edges`, {
                method: 'POST', headers: accountHeaders(true),
                body: JSON.stringify({
                    map_id: MAP_ID,
                    from_id: fromRoomId,
                    to_id: toRoomId,
                    edge_type: edgeType,
                    label: { stairs:'楼梯', elevator:'电梯', portal:'传送门' }[edgeType] || ''
                })
            });
            await fetchMapData();
        };

        // ── 实体圆点：点击选中 / 保存房间绑定 ──────────────────
        const mapSelectEntityDot = (entity) => {
            mapSelectedEntityDot.value = { ...entity };
            mapSelectedRoom.value = null;
            mapSelectedEdge.value = null;
        };

        const mapSaveEntityRoom = async (entity) => {
            await fetch(`${API_BASE_URL}/api/world-entities/${entity.id}/room?room_id=${entity.room_id ?? ''}`,
                { method: 'PUT', headers: accountHeaders() }
            ).catch(() => {});
            // 同步到本地 worldEntities 列表
            const idx = worldEntities.value.findIndex(e => e.id === entity.id);
            if (idx >= 0) worldEntities.value[idx].room_id = entity.room_id;
        };

        // ── 时间线移动房间 ───────────────────────────────────────
        const mapSetTimelineRoom = async (tlId, roomId) => {
            await fetch(`${API_BASE_URL}/api/map/move?room_id=${roomId}&timeline_id=${tlId}`,
                { method: 'POST', headers: accountHeaders() }
            ).catch(() => {});
            const tl = timelines.value.find(t => t.id === tlId);
            if (tl) tl.current_room_id = roomId;
            // 同步刷新地图（探索状态）
            const r = mapRooms.value.find(r => r.id === roomId);
            if (r && r.state === 'unknown') r.state = 'explored';
        };

        // ══════════════════════════════════════════════════════
        // 【顶栏资料册】：玩家友好的查看层
        // ══════════════════════════════════════════════════════
        const showDossierModal = ref(false);
        const dossierTab = ref('worldview');
        const dossierSearch = ref('');
        const dossierSelectedDocId = ref(null);
        const dossierSelectedDoc = ref(null);
        const dossierDocChunks = ref([]);
        const dossierLoadingDoc = ref(false);
        const dossierAssets = ref([]);
        const dossierAssetsLoading = ref(false);
        const dossierAssetPreview = ref(null);
        const dossierAssetPreviewIndex = ref(0);
        const dossierAssetZoom = ref(1);
        const dossierAssetPanX = ref(0);
        const dossierAssetPanY = ref(0);
        const dossierSelectedRoom = ref(null);
        const dossierSelectedEntity = ref(null);
        const dossierMapCanvasRef = ref(null);
        const dossierMapScale = ref(1);
        const dossierMapViewX = ref(0);
        const dossierMapViewY = ref(0);
        const dossierMapSvgW = ref(900);
        const dossierMapSvgH = ref(620);
        const dossierMapViewBox = computed(() =>
            `${dossierMapViewX.value} ${dossierMapViewY.value} ${dossierMapSvgW.value / dossierMapScale.value} ${dossierMapSvgH.value / dossierMapScale.value}`
        );
        let dossierMapPanning = false;
        let dossierMapPanStart = { x: 0, y: 0, vx: 0, vy: 0 };
        let dossierMapSuppressClick = false;
        let dossierAssetPanning = false;
        let dossierAssetPanStart = { x: 0, y: 0, px: 0, py: 0 };

        const dossierTabs = [
            { id: 'worldview', label: '世界观', icon: 'ph-globe' },
            { id: 'map', label: '地图', icon: 'ph-map-trifold' },
            { id: 'assets', label: '图片', icon: 'ph-images' },
            { id: 'knowledge', label: '知识库', icon: 'ph-database' },
            { id: 'lore', label: '百科', icon: 'ph-books' },
            { id: 'entities', label: '实体', icon: 'ph-graph' },
            { id: 'memory', label: '记忆流', icon: 'ph-brain' },
        ];
        const dossierActiveTab = computed(() =>
            dossierTabs.find(tab => tab.id === dossierTab.value) || dossierTabs[0]
        );
        const dossierTabCount = (tabId) => {
            if (tabId === 'map') return mapRooms.value.length || '';
            if (tabId === 'assets') return dossierAssets.value.length || '';
            if (tabId === 'knowledge') return ragDocuments.value.filter(d => !d.hidden).length || '';
            if (tabId === 'lore') return lorebook.value.length || '';
            if (tabId === 'entities') return worldEntities.value.length || '';
            return '';
        };
        const dossierVisibleRagDocuments = computed(() => {
            const q = dossierSearch.value.toLowerCase();
            return ragDocuments.value
                .filter(doc => !doc.hidden)
                .filter(doc => !q || `${doc.title || ''} ${doc.source || ''}`.toLowerCase().includes(q));
        });
        const dossierFilteredLore = computed(() => {
            const q = dossierSearch.value.toLowerCase();
            return lorebook.value.filter(item =>
                !q || `${item.keywords || ''} ${item.content || ''}`.toLowerCase().includes(q)
            );
        });
        const dossierFilteredEntities = computed(() => {
            const q = dossierSearch.value.toLowerCase();
            return worldEntities.value.filter(entity =>
                !q || `${entity.name || ''} ${entity.location || ''} ${entity.status || ''} ${entity.state_desc || ''}`.toLowerCase().includes(q)
            );
        });
        const dossierFilteredAssets = computed(() => {
            const q = dossierSearch.value.toLowerCase();
            return dossierAssets.value.filter(asset => {
                const sceneText = (asset.scenes || []).map(scene => scene.name || '').join(' ');
                return !q || `${asset.name || ''} ${sceneText}`.toLowerCase().includes(q);
            });
        });
        const dossierDocText = computed(() =>
            dossierDocChunks.value.map(chunk => chunk.chunk_text).filter(Boolean).join('\n\n')
        );
        const dossierAssetTransform = computed(() =>
            `translate3d(${dossierAssetPanX.value}px, ${dossierAssetPanY.value}px, 0) scale(${dossierAssetZoom.value})`
        );
        const formatAssetSize = (bytes = 0) => {
            const n = Number(bytes || 0);
            if (!n) return '0 B';
            if (n < 1024) return `${n} B`;
            if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
            return `${(n / 1024 / 1024).toFixed(1)} MB`;
        };
        const escapeHtml = (value = '') => String(value ?? '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
        const cleanCampaignDisplayText = (value = '') => {
            let source = String(value || '').replace(/\r\n?/g, '\n');
            if (!source) return '';
            const images = [];
            source = source.replace(/!\[[^\]]*\]\(\/api\/campaign-assets\/[^)]+\)/g, (match) => {
                images.push(match);
                return `@@ZRIC_IMAGE_${images.length - 1}@@`;
            });
            source = source
                .replace(/<table\b[^>]*>[\s\S]*?<\/table>/gi, '\n')
                .replace(/<br\s*\/?>/gi, '\n')
                .replace(/<\/(?:p|div|li|tr|h[1-6])\s*>/gi, '\n')
                .replace(/<[^>]+>/g, '')
                .replace(/\$=\s*\\mathbf\{([^}]+)\}\s*=\$/g, '= $1 =')
                .replace(/\$\\mathbf\{([^}]+)\}\$/g, '$1')
                .replace(/\\([*_{}\[\]()#+.!-])/g, '$1')
                .replace(/^\s*(?:类型\s*[:：]\s*\w+|材料\s*\d+)\s*$/gmi, '')
                .replace(/[ \t]+\n/g, '\n')
                .replace(/\n{3,}/g, '\n\n')
                .trim();
            images.forEach((image, idx) => {
                source = source.replace(`@@ZRIC_IMAGE_${idx}@@`, image);
            });
            return source;
        };
        const renderCampaignRichText = (value = '', imageClass = 'dossier-image-block') => {
            const source = cleanCampaignDisplayText(value);
            const parts = [];
            let last = 0;
            const imageRe = /!\[([^\]]*)\]\((\/api\/campaign-assets\/[^)]+)\)/g;
            let match;
            while ((match = imageRe.exec(source)) !== null) {
                if (match.index > last) parts.push(escapeHtml(source.slice(last, match.index)));
                const alt = escapeHtml(match[1] || '剧本图片');
                const src = escapeHtml(match[2].replace(/\s+/g, ''));
                parts.push(`<figure class="${imageClass}"><img src="${src}" alt="${alt}" loading="lazy"><figcaption>${alt}</figcaption></figure>`);
                last = match.index + match[0].length;
            }
            if (last < source.length) parts.push(escapeHtml(source.slice(last)));
            return parts.join('').replace(/\n/g, '<br>');
        };
        const renderDossierRichText = (value = '') => renderCampaignRichText(value, 'dossier-image-block');
        const renderSceneRichText = (value = '') => renderCampaignRichText(value, 'scene-image-block');

        const selectDossierDoc = async (docId) => {
            dossierSelectedDocId.value = docId;
            dossierSelectedDoc.value = null;
            dossierDocChunks.value = [];
            if (!docId) return;
            dossierLoadingDoc.value = true;
            try {
                const r = await fetch(`${API_BASE_URL}/api/rag/documents/${docId}`);
                const d = await r.json();
                if (d.status === 'success') {
                    dossierSelectedDoc.value = d.document;
                    dossierDocChunks.value = d.chunks || [];
                }
            } catch(e) {}
            finally { dossierLoadingDoc.value = false; }
        };

        const fetchCampaignAssets = async () => {
            dossierAssetsLoading.value = true;
            try {
                const r = await fetch(`${API_BASE_URL}/api/game/campaign-assets`, { headers: accountHeaders() });
                const d = await r.json();
                if (d.status === 'success') {
                    dossierAssets.value = Array.isArray(d.assets) ? d.assets : [];
                    if (dossierAssetPreview.value) {
                        const refreshed = dossierAssets.value.find(asset => asset.name === dossierAssetPreview.value.name);
                        if (refreshed) dossierAssetPreview.value = refreshed;
                        else closeDossierAssetPreview();
                    }
                }
            } catch(e) {
                dossierAssets.value = [];
            } finally {
                dossierAssetsLoading.value = false;
            }
        };

        const openDossierAssetPreview = (asset) => {
            if (!asset) return;
            const idx = dossierFilteredAssets.value.findIndex(item => item.name === asset.name);
            dossierAssetPreviewIndex.value = idx >= 0 ? idx : 0;
            dossierAssetPreview.value = asset;
            resetDossierAssetView();
        };
        const closeDossierAssetPreview = () => {
            dossierAssetPreview.value = null;
            resetDossierAssetView();
        };
        const stepDossierAssetPreview = (delta) => {
            const list = dossierFilteredAssets.value;
            if (!list.length) return closeDossierAssetPreview();
            const next = (dossierAssetPreviewIndex.value + delta + list.length) % list.length;
            dossierAssetPreviewIndex.value = next;
            dossierAssetPreview.value = list[next];
            resetDossierAssetView();
        };
        const resetDossierAssetView = () => {
            dossierAssetZoom.value = 1;
            dossierAssetPanX.value = 0;
            dossierAssetPanY.value = 0;
            dossierAssetPanning = false;
        };
        const setDossierAssetZoom = (value) => {
            const next = Math.min(5, Math.max(0.5, Number(value) || 1));
            dossierAssetZoom.value = Math.round(next * 100) / 100;
            if (dossierAssetZoom.value <= 1) {
                dossierAssetPanX.value = 0;
                dossierAssetPanY.value = 0;
            }
        };
        const zoomDossierAsset = (delta) => {
            setDossierAssetZoom(dossierAssetZoom.value + delta);
        };
        const toggleDossierAssetZoom = () => {
            if (dossierAssetZoom.value <= 1.05) setDossierAssetZoom(2);
            else resetDossierAssetView();
        };
        const dossierAssetEventPoint = (e) => {
            if (e?.touches?.length) return e.touches[0];
            if (e?.changedTouches?.length) return e.changedTouches[0];
            return e || { clientX: 0, clientY: 0 };
        };
        const dossierAssetOnWheel = (e) => {
            zoomDossierAsset(e.deltaY > 0 ? -0.15 : 0.15);
        };
        const dossierAssetStartPan = (e) => {
            if (dossierAssetZoom.value <= 1) return;
            if (e.button !== undefined && e.button !== 0) return;
            const p = dossierAssetEventPoint(e);
            dossierAssetPanning = true;
            dossierAssetPanStart = {
                x: p.clientX,
                y: p.clientY,
                px: dossierAssetPanX.value,
                py: dossierAssetPanY.value,
            };
        };
        const dossierAssetMovePan = (e) => {
            if (!dossierAssetPanning) return;
            const p = dossierAssetEventPoint(e);
            dossierAssetPanX.value = dossierAssetPanStart.px + (p.clientX - dossierAssetPanStart.x);
            dossierAssetPanY.value = dossierAssetPanStart.py + (p.clientY - dossierAssetPanStart.y);
        };
        const dossierAssetEndPan = () => {
            dossierAssetPanning = false;
        };

        const refreshDossierTabData = async (tab = dossierTab.value) => {
            if (tab === 'worldview') await fetchWorldview();
            if (tab === 'map') {
                await Promise.all([fetchMapData(), fetchWorldEntities(), fetchTimelines()]);
                scheduleDossierMapFit();
            }
            if (tab === 'assets') await fetchCampaignAssets();
            if (tab === 'knowledge') {
                await fetchRagDocuments();
                const firstDoc = dossierVisibleRagDocuments.value[0];
                if (!dossierSelectedDocId.value && firstDoc) await selectDossierDoc(firstDoc.id);
            }
            if (tab === 'lore') await fetchLorebook();
            if (tab === 'entities') {
                await Promise.all([fetchWorldEntities(), fetchMapData()]);
            }
            if (tab === 'memory') await fetchMemory();
        };

        const switchDossierTab = async (tab) => {
            dossierTab.value = tab;
            dossierSearch.value = '';
            await refreshDossierTabData(tab);
        };

        const openDossierPanel = async (tab = 'worldview') => {
            closeAdvancedSystemPanels();
            showGameSettingsModal.value = false;
            dossierTab.value = tab;
            dossierSearch.value = '';
            showDossierModal.value = true;
            await refreshDossierTabData(tab);
        };

        const updateDossierMapSize = () => {
            const rect = dossierMapCanvasRef.value?.getBoundingClientRect();
            dossierMapSvgW.value = Math.max(320, rect?.width || dossierMapCanvasRef.value?.clientWidth || dossierMapSvgW.value || 900);
            dossierMapSvgH.value = Math.max(320, rect?.height || dossierMapCanvasRef.value?.clientHeight || dossierMapSvgH.value || 620);
            return Boolean(rect && rect.width > 8 && rect.height > 8);
        };

        const scheduleDossierMapFit = () => {
            nextTick(() => {
                requestAnimationFrame(() => {
                    if (!updateDossierMapSize()) {
                        setTimeout(() => dossierFitMapToFloor(), 60);
                        return;
                    }
                    dossierFitMapToFloor();
                });
            });
        };

        const dossierFitMapToFloor = () => {
            if (!updateDossierMapSize()) {
                setTimeout(() => dossierFitMapToFloor(), 60);
                return;
            }
            const rooms = mapVisibleRooms.value;
            if (!rooms.length) {
                dossierMapScale.value = 1;
                dossierMapViewX.value = 0;
                dossierMapViewY.value = 0;
                dossierSelectedRoom.value = null;
                return;
            }
            const minX = Math.min(...rooms.map(r => r.x));
            const minY = Math.min(...rooms.map(r => r.y));
            const maxX = Math.max(...rooms.map(r => r.x + r.w));
            const maxY = Math.max(...rooms.map(r => r.y + r.h));
            const pad = 90;
            const boundsW = Math.max(1, maxX - minX);
            const boundsH = Math.max(1, maxY - minY);
            const scale = Math.min(
                dossierMapSvgW.value / (boundsW + pad * 2),
                dossierMapSvgH.value / (boundsH + pad * 2)
            );
            dossierMapScale.value = Math.min(2.2, Math.max(0.25, scale));
            const viewW = dossierMapSvgW.value / dossierMapScale.value;
            const viewH = dossierMapSvgH.value / dossierMapScale.value;
            dossierMapViewX.value = minX + boundsW / 2 - viewW / 2;
            dossierMapViewY.value = minY + boundsH / 2 - viewH / 2;
        };

        const selectDossierFloor = (floor) => {
            mapActiveFloor.value = floor;
            dossierSelectedRoom.value = null;
            scheduleDossierMapFit();
        };

        const dossierSelectMapRoom = (room, fromList = false) => {
            if (!fromList && dossierMapSuppressClick) return;
            dossierSelectedRoom.value = { ...room };
            dossierSelectedEntity.value = null;
        };

        const dossierMapEventPoint = (e) => {
            if (e?.touches?.length) return e.touches[0];
            if (e?.changedTouches?.length) return e.changedTouches[0];
            return e || { clientX: 0, clientY: 0 };
        };

        const dossierMapClientToSvg = (e) => {
            const p = dossierMapEventPoint(e);
            const rect = dossierMapCanvasRef.value?.getBoundingClientRect();
            if (!rect) return { x: 0, y: 0 };
            return {
                x: (p.clientX - rect.left) / dossierMapScale.value + dossierMapViewX.value,
                y: (p.clientY - rect.top) / dossierMapScale.value + dossierMapViewY.value,
            };
        };

        const dossierMapOnWheel = (e) => {
            updateDossierMapSize();
            const before = dossierMapClientToSvg(e);
            const nextScale = Math.min(3, Math.max(0.2, dossierMapScale.value * (e.deltaY > 0 ? 0.9 : 1.1)));
            dossierMapScale.value = nextScale;
            const rect = dossierMapCanvasRef.value?.getBoundingClientRect();
            if (rect) {
                dossierMapViewX.value = before.x - (e.clientX - rect.left) / nextScale;
                dossierMapViewY.value = before.y - (e.clientY - rect.top) / nextScale;
            }
        };

        const dossierMapStartPan = (e) => {
            if (e.button !== undefined && e.button !== 0) return;
            const p = dossierMapEventPoint(e);
            updateDossierMapSize();
            dossierMapPanning = true;
            dossierMapSuppressClick = false;
            dossierMapPanStart = { x: p.clientX, y: p.clientY, vx: dossierMapViewX.value, vy: dossierMapViewY.value, dragged: false };
        };
        const dossierMapMovePan = (e) => {
            if (!dossierMapPanning) return;
            const p = dossierMapEventPoint(e);
            const dx = p.clientX - dossierMapPanStart.x;
            const dy = p.clientY - dossierMapPanStart.y;
            dossierMapPanStart.dragged = dossierMapPanStart.dragged || Math.hypot(dx, dy) > 4;
            dossierMapViewX.value = dossierMapPanStart.vx - dx / dossierMapScale.value;
            dossierMapViewY.value = dossierMapPanStart.vy - dy / dossierMapScale.value;
        };
        const dossierMapEndPan = () => {
            if (dossierMapPanStart.dragged) {
                dossierMapSuppressClick = true;
                setTimeout(() => { dossierMapSuppressClick = false; }, 80);
            }
            dossierMapPanning = false;
        };
        const dossierMapOnMouseDown = (e) => dossierMapStartPan(e);
        const dossierMapOnMouseMove = (e) => dossierMapMovePan(e);
        const dossierMapOnMouseUp = () => dossierMapEndPan();
        const dossierMapOnTouchStart = (e) => dossierMapStartPan(e);
        const dossierMapOnTouchMove = (e) => dossierMapMovePan(e);
        const dossierMapOnTouchEnd = () => dossierMapEndPan();

        return {
            appState, playSurface, pendingLaunchMode, isLoading, isDeletingCampaign, campaignPackageImportBusy, campaignPackageExportBusy, dbConnected, campaignFiles, saveFiles, selectedCampaign, selectedCampaignInfo, campaignLoadSummary,
            activeSoloSessionId, soloSessionLink, leaveGameToMenu, switchToGmSurface, copySoloRoomLink, saveSoloRoomProgress,
            showGameSettingsModal, showAdvancedSettings, openGameSettingsModal, returnToAdvancedSettings,
            showCampaignImportModal, campaignImportName, campaignImportMainFile,
            campaignImportAssets, campaignImportOcrEnabled, campaignImportBusy, campaignImportResult,
            campaignImportWarnings, campaignImportErrorText, campaignImportVisibleWarnings, campaignImportVerifiedNotice,
            campaignImportOutcome,
            campaignImportProgress, campaignImportProgressStep, campaignImportProgressMessage,
            campaignImportFormatsText, campaignImportUsesMinerU, campaignImportMinerUModeText,
            openCampaignImportModal, campaignImportPickMain,
            campaignImportPickAssets, importCampaign, reparseSelectedCampaign,
            // AI 模型
            aiModels, aiModelSearch, activeAiModel, aiModelDraft, openAiModelDropdownState, isFetchingAiModels, aiModelError, activeProviderName, fetchAiModels, switchAiModel, openAiModelDropdown, toggleAiModelDropdown, filteredAiModels, selectAiModel, selectFirstFilteredAiModel, applyAiModelDraft, modelAccentColor, modelAccentRgb, modelIcon, cycleModel,
            showApiKeyPanel, openApiKeyPanel, apiKeyStatus, apiKeyInputs, apiProviders, activeProviderId, apiKeysMissing,
            tokenPolicy, isSavingTokenPolicy, activeTokenPolicyMode, tokenPolicySummary, setTokenPolicyMode,
            aiCache, aiCacheSummary, isSavingAiCache, fetchAiCacheStatus, setAiCacheEnabled, clearAiCache,
            isSavingKeys, apiKeySaveMsg, apiKeySaveOk, dropdownModelSearches, openModelDropdownCapability, modelOptions, modelConfigFields, isFetchingConfigModels, fetchingConfigCapability,
            fetchApiKeyStatus, saveApiKeys, newApiProvider, switchApiProvider, deleteApiProvider, fetchConfigModels, fetchAllConfigModels, selectConfigModel, openModelDropdown, toggleModelDropdown, filteredConfigModels, selectFirstFilteredConfigModel,
            storyNodes, characters, currentNode, isEditMode, hpLabel, sanLabel, editData, newOptionText, newOptionTarget, showWorldviewModal, worldviewContent, showMemoryModal, memoryContent, showLorebookModal, lorebook, currentLore, aiGeneratedText, playerAction, actionType, soloPlayerName, soloSelectedCharacterIds, soloPlayableCharacters, soloSelectedNames, soloConfirmedNames, soloHasConfirmedCharacters, soloActorLabel, soloVisibleCharacters, soloPerspectiveCharacter, soloPublicSceneText, soloSceneText, soloCharacterBriefingText, showSoloCharacterBriefing, soloVisibleOptions, soloActionLog, visibleSoloActionLog, soloLogRef, isSoloCharacterSelected, isSoloCharacterLocked, toggleSoloCharacter, clearSoloCharacters, confirmSoloCharacters, saveSoloProfile, submitSoloAction, selectSoloVisibleOption, isGeneratingText, checkpointCount, isRollingBack, goBack, isGeneratingOptions, imgPrompt, imgStyle, imgModel, isGeneratingImage, isLoadingImg, generatedImageUrl, imgEnPrompt, imgPromptUsed, imgLoadError, audioRef, tracks, currentTrackId, currentTrackUrl, customTrackUrl, isPlaying, volume, narrativeMood, timeSkipInput, forceNarrativeThrust, optionLikelihoods, fateSpinning, fateHighlightIdx, dicePanel, diceBusy, diceError, diceResultText, rollGmDice, clearDiceResult, showTriggerModal, triggers, currentTrigger, condTypes, collapsedChars, toggleCharCollapse, showCharModal, isGeneratingNPC, isExpandingBranch, expandingBranchText, newChar, npcToast, triggerAlert, passiveAlerts, statChangesLog, showStatChanges, showBattleReportModal, battleReport, isGeneratingReport, battleReportFilename, leftTab, treeContainerRef, treeLayout,
            gmManualEventText, gmManualEventKind, gmManualEventBusy, gmManualEventMsg, gmManualSyncPlayer, gmManualRecordMemory, publishGmManualEvent,
            // 多人房间
            showMultiplayerModal, multiplayerRoom, multiplayerMembers, multiplayerMessages,
            multiplayerBusy, multiplayerError, multiplayerStatusMsg, multiplayerWsConnected,
            multiplayerRoomName, multiplayerJoinCode, multiplayerProfileName, multiplayerInviteUrl,
            multiplayerMaxPlayers, multiplayerPlayableCharacterLimit, multiplayerMaxPlayersUpper, multiplayerRoomMaxPlayers,
            multiplayerClaimedPlayerCount, multiplayerRoomSeatsRemaining, clampMultiplayerMaxPlayers,
            multiplayerAuthAccount, multiplayerAuthForm, multiplayerAuthBusy, multiplayerAuthMsg, submitMultiplayerAuth, logoutMultiplayerAuth,
            openMultiplayerModal, openMenuMultiplayer, createMultiplayerRoom, createMultiplayerRoomForSelectedCampaign, joinMultiplayerRoom, enterMultiplayerRoomByCode, copyMultiplayerInvite, openMultiplayerTable, saveCurrentMultiplayerRoomProgress, canSaveCurrentMultiplayerRoom,
            // 时间线系统
            timelinePanelOpen, timelines, showTlEditModal, tlEditData, tlColorPresets,
            showTlMemoryModal, tlMemoryTarget, tlMemoryContent,
            showTlDynamicModal, tlDynamicTarget, tlDynamicAction, isTlDynamicRunning,
            showMergeModal, mergeSource, mergeTargetId, isMerging,
            hexToRgb,
            // 世界实体系统
            showWorldEntitiesModal, worldEntities, currentEntity, entityFilter, filteredEntities,
            // 分屏系统
            activeTimelines, tlActionInputs, tlActionTypes, getTlActionType, tlLastContexts, tlRunningIds, runTlDynamicInline, tlJumpAndBind,
            // RAG 知识库
            showRagModal, ragTab, ragDocuments, ragSelectedDoc, ragForm,
            ragIngesting, ragIngestResult, ragSearchQuery, ragSearching, ragSearchResults,
            ragUploadFile, ragDragOver, ragChunkSize, ragChunkOverlap, ragTopK,
            openRagModal, ragImport, ragDeleteDoc, ragToggleHidden, ragSearch,
            ragHandleFileSelect, ragHandleDrop,
            fetchCampaigns, fetchSaves, onCampaignSelectionChanged, deleteSelectedCampaign, exportCampaignPackage, campaignPackagePickImport, loadAndStart, exportSave, doExportOverwrite, doExportNew,
            showExportModal, exportShowNameInput, exportNewName, currentSaveFolder, currentSavePath, lockedSaveSourceLabel, lockedSaveSourcePath,
            isExportingSave, isRenamingSave, renamingSaveName, renameSaveDraft, saveManagerMsg, saveManagerOk, saveSearch, menuSaveModeFilter, menuSaveModeCounts, currentSaveItem, currentSaveWritable, filteredSaveItems, menuSaveItems,
            saveStatsText, savePlayModeLabel, saveDisplayName, downloadSave, deleteSave, startRenameSave, cancelRenameSave, renameSave, loadSaveFromManager, restoreSaveFromMenu,
            fetchGameState, jumpToNode, saveNodeChanges, createNewNode, deleteCurrentNode, addManualOption, deleteOption, saveStatLabels, saveCharacterState, createCharacter, deleteCharacter, generateNPC, resetNewChar, openWorldviewModal, saveWorldview, openMemoryModal, saveMemory, openLorebookModal, saveLore, deleteLore, fetchTriggers, openTriggerModal, newTrigger, selectTrigger, saveTrigger, deleteTrigger, resetTriggerFired, addTriggerAction, resetActionFields, resetAllTriggers, generateAIText, generateDynamicOptions, openBattleReportModal, generateBattleReport, downloadBattleReport, generateImage, togglePlay, changeTrack, doTimeSkip, doForceThrust, fatePick,
            // GM 干预
            gmCorrection, lastDynamicContext, retryWithCorrection,
            // NPC Persona
            showPersonaModal, personaTarget, personaMbti, personaQuirks, personaNote,
            mbtiPool, quirkPool, openPersonaModal, openPersonaModalFull, buildPersonaString, savePersona, savePersonaFull, clearPersona,
            getEntityEmotion, getEntityMemories, personaBreakpoint, personaMemories, personaEmotion, removePersonaMemory,
            // 时间线方法
            openTimelinePanel, openNewTimelineModal, editTimeline, toggleTlChar, saveTlEdit, deleteTimeline, bindCurrentSceneToTimeline, openTlMemory, saveTlMemory, openTlDynamicModal, runTlDynamic, openMergeModal, runMerge,
            showTlRoomModal, tlRoomTarget, tlRoomSelected, openTlRoomModal, saveTlRoom,
            // 世界实体方法
            openWorldEntitiesPanel, openNewEntityForm, saveEntity, deleteEntity,
            // 顶栏资料册
            showDossierModal, dossierTab, dossierTabs, dossierActiveTab, dossierSearch,
            dossierSelectedDocId, dossierSelectedDoc, dossierDocChunks, dossierLoadingDoc,
            dossierAssets, dossierAssetsLoading, dossierFilteredAssets,
            dossierAssetPreview, dossierAssetPreviewIndex,
            dossierAssetZoom, dossierAssetPanX, dossierAssetPanY, dossierAssetTransform,
            dossierVisibleRagDocuments, dossierFilteredLore, dossierFilteredEntities, dossierDocText,
            formatAssetSize, renderDossierRichText, renderSceneRichText,
            dossierSelectedRoom, dossierSelectedEntity, dossierMapCanvasRef,
            dossierMapScale, dossierMapViewX, dossierMapViewY, dossierMapSvgW, dossierMapSvgH, dossierMapViewBox,
            dossierTabCount, openDossierPanel, switchDossierTab,
            selectDossierDoc, fetchCampaignAssets, openDossierAssetPreview, closeDossierAssetPreview, stepDossierAssetPreview,
            resetDossierAssetView, zoomDossierAsset, toggleDossierAssetZoom,
            dossierAssetOnWheel, dossierAssetStartPan, dossierAssetMovePan, dossierAssetEndPan,
            selectDossierFloor, dossierFitMapToFloor, dossierSelectMapRoom,
            dossierMapOnWheel, dossierMapOnMouseDown, dossierMapOnMouseMove, dossierMapOnMouseUp,
            dossierMapOnTouchStart, dossierMapOnTouchMove, dossierMapOnTouchEnd,
            // 地图系统
            MAP_GRID, showMapModal, mapRooms, mapEdges, mapFloors, mapActiveFloor,
            mapVisibleRooms, mapVisibleEdges,
            mapSelectedRoom, mapSelectedEdge,
            mapActiveTool, mapSnapGrid, mapScale, mapViewX, mapViewY, mapSvgW, mapSvgH,
            mapMouseSvg, mapDrawingRoom, mapDrawStart, mapNamingRoom, mapNamingLabel,
            mapNamingPos, mapConnectSource, mapDrawingEdge, mapCanvasRef, mapNameInputRef,
            mapTools, mapRoomStates, mapRoomColors,
            mapRoomById, openMapModal, fetchMapData, mapOnWheel, mapOnCanvasMousedown,
            mapOnCanvasMousemove, mapOnCanvasMouseup, mapOnRoomMousedown, mapOnRoomClick,
            mapSelectEdge, mapConfirmRoom, mapCancelRoom, mapSaveRoom, mapSaveEdge,
            mapSetRoomState, mapSetActive, mapDeleteSelectedRoom, mapDeleteSelectedEdge, mapClearAll,
            mapAddFloor, mapRemoveFloor, mapMoveRoomToFloor, mapConnectCrossFloor,
            mapSelectedEntityDot, mapSelectEntityDot, mapSaveEntityRoom, mapSetTimelineRoom
        };
    }
}).mount('#app');
