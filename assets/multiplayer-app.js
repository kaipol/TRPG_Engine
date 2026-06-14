const { createApp, nextTick } = Vue;
    const API_BASE_URL = window.location.protocol.startsWith('http') ? window.location.origin : 'http://localhost:8000';
    const readStoredJson = (storage, key, fallback) => {
      try { return JSON.parse(storage.getItem(key) || '') || fallback; }
      catch (_) { return fallback; }
    };
    const readOrCreateClientId = () => {
      let id = localStorage.getItem('zric-mp-client-id') || '';
      if (!id) {
        id = `client-${crypto.randomUUID()}`;
        localStorage.setItem('zric-mp-client-id', id);
      }
      return id;
    };

    createApp({
      data() {
        const saved = readStoredJson(localStorage, 'zric-mp-profile', {});
        const tabSaved = readStoredJson(sessionStorage, 'zric-mp-tab-profile', {});
        const tabTokens = readStoredJson(sessionStorage, 'zric-mp-tab-tokens', {});
        const storedTokens = readStoredJson(localStorage, 'zric-mp-tokens', {});
        const savedAccount = readStoredJson(localStorage, 'zric-auth-account', null);
        const clientId = readOrCreateClientId();
        return {
          room: null,
          members: [],
          messages: [],
          characterClaims: [],
          gameState: {},
          ws: null,
          wsConnected: false,
          joinBusy: false,
          actionBusy: false,
          showSettings: false,
          hpLabel: 'HP',
          sanLabel: 'SAN',
          profile: {
            id: tabSaved.id || saved.id || crypto.randomUUID(),
            name: tabSaved.name || (saved.name && saved.name !== 'GM' ? saved.name : '玩家') || '玩家',
            role: 'player',
            color: tabSaved.color || saved.color || '#7dd3fc',
          },
          clientId,
          memberTokens: { ...(storedTokens.memberTokens || {}), ...(tabTokens.memberTokens || {}) },
          tabInstanceId: crypto.randomUUID(),
          presenceTimer: null,
          joinCode: '',
          actionText: '',
          publicChatText: '',
          actionType: 'mixed',
          pendingOption: null,
          activeThreadPlayerId: '',
          selectedCharacterIds: Array.isArray(tabSaved.characterIds) ? tabSaved.characterIds : [],
          characterClaimBusy: false,
          characterClaimMsg: '',
          checkpointCount: 0,
          rollbackBusy: false,
          accountToken: localStorage.getItem('zric-auth-token') || '',
          account: savedAccount,
          authMode: 'login',
          authForm: { username: '', password: '', display_name: '' },
          authBusy: false,
          authMsg: '',
          bgmPlaying: false,
          bgmVolume: 0.3,
          showDossierModal: false,
          dossierTab: 'worldview',
          dossierSearch: '',
          dossierTabs: [
            { id: 'worldview', label: '世界观', icon: 'ph-globe' },
            { id: 'map', label: '地图', icon: 'ph-map-trifold' },
            { id: 'knowledge', label: '知识库', icon: 'ph-database' },
            { id: 'lore', label: '百科', icon: 'ph-books' },
            { id: 'entities', label: '实体', icon: 'ph-graph' },
            { id: 'memory', label: '记忆流', icon: 'ph-brain' },
          ],
          worldviewContent: '',
          memoryContent: '',
          lorebook: [],
          worldEntities: [],
          timelines: [],
          ragDocuments: [],
          dossierSelectedDocId: null,
          dossierSelectedDoc: null,
          dossierDocChunks: [],
          dossierLoadingDoc: false,
          mapRooms: [],
          mapEdges: [],
          mapFloors: [1],
          mapActiveFloor: 1,
          dossierSelectedRoom: null,
          dossierSelectedEntity: null,
          MAP_GRID: 40,
          dossierMapScale: 1,
          dossierMapViewX: 0,
          dossierMapViewY: 0,
          dossierMapSvgW: 900,
          dossierMapSvgH: 620,
          dossierMapPanning: false,
          dossierMapPanStart: { x: 0, y: 0, vx: 0, vy: 0, dragged: false },
          dossierMapSuppressClick: false,
        };
      },
      watch: {
        profile: { deep: true, handler() { this.saveProfile(); } },
        selectedCharacterIds() { this.saveProfile(); },
        bgmVolume(value) {
          const el = this.$refs.audioEl;
          if (el) el.volume = Number(value || 0.3);
        },
        bgmUrl(nextUrl, prevUrl) {
          if (nextUrl !== prevUrl && this.bgmPlaying) this.playBgm();
        },
      },
      computed: {
        currentScene() { return this.gameState?.current_scene || null; },
        currentRoom() { return this.gameState?.current_room || null; },
        bgmUrl() { return this.gameState?.bgm_url || ''; },
        bgmName() { return this.gameState?.bgm_name || (this.bgmUrl ? '环境音乐' : '等待 GM 或 AI-GM 选择音乐'); },
        publicSceneText() {
          const scene = this.currentScene || {};
          return scene.expanded_content || this.gameState?.scene_ai_text || scene.content || '';
        },
        characterOpeningMessage() {
          return [...this.messages].reverse().find(msg => {
            const payload = msg?.payload || {};
            return payload.source === 'character_opening' && this.messageBelongsToMe(msg);
          }) || null;
        },
        characterOpeningText() {
          const message = this.characterOpeningMessage;
          if (message) return message.payload?.opening || message.content || '';
          return this.localCharacterOpeningText();
        },
        hasPostOpeningSceneActivity() {
          const openingId = Number(this.characterOpeningMessage?.id || 0);
          if (!openingId) return false;
          return this.messages.some(msg => {
            if (Number(msg.id || 0) <= openingId) return false;
            const payload = msg.payload || {};
            if (payload.source === 'character_opening') return false;
            return ['action', 'ai', 'dice', 'state'].includes(msg.kind);
          });
        },
        showCharacterOpening() {
          if (!this.characterOpeningText) return false;
          const openingSceneId = Number(this.characterOpeningMessage?.payload?.scene_id || 0);
          const currentSceneId = Number(this.gameState?.current_scene_id || this.currentScene?.id || 0);
          if (openingSceneId && currentSceneId && openingSceneId !== currentSceneId) return false;
          return !this.hasPostOpeningSceneActivity;
        },
        primarySceneTitle() {
          return this.showCharacterOpening ? `${this.actorName} 的开场` : (this.currentScene?.name || '等待开场');
        },
        primarySceneText() {
          return this.showCharacterOpening ? this.characterOpeningText : this.publicSceneText;
        },
        sceneText() {
          return this.primarySceneText;
        },
        visibleActionOptions() {
          return this.characterPerspectiveOptions(
            this.selectedCharacters[0],
            this.currentScene?.options || [],
            this.publicSceneText
          );
        },
        sceneImage() {
          const scene = this.currentScene || {};
          return this.gameState?.scene_image || scene.scene_image || '';
        },
        allCharacters() { return this.gameState?.all_characters || this.gameState?.characters || []; },
        visibleCharacters() { return this.gameState?.characters || this.allCharacters; },
        playableCharacters() {
          const listed = this.gameState?.playable_characters;
          const base = Array.isArray(listed) && listed.length ? listed : this.visibleCharacters;
          const active = base.filter(char => (char.status || 'active') !== 'hidden');
          return active.length ? active : base;
        },
        myClaims() {
          const playerId = String(this.profile.id || '');
          return this.characterClaims.filter(claim => String(claim.player_id || '') === playerId);
        },
        lockedCharacterIds() { return this.myClaims.map(claim => claim.character_id); },
        hasLockedCharacters() { return this.lockedCharacterIds.length > 0; },
        selectedCharacters() {
          const sourceIds = this.hasLockedCharacters ? this.lockedCharacterIds : this.selectedCharacterIds;
          const selectedIds = new Set(sourceIds.map(id => String(id)));
          const sourceCharacters = this.hasLockedCharacters ? this.allCharacters : this.playableCharacters;
          return sourceCharacters.filter(char => selectedIds.has(String(char.id)));
        },
        selectedCharacterNames() { return this.selectedCharacters.map(char => char.name); },
        visibleStatusCharacters() { return this.selectedCharacters.length ? this.selectedCharacters : this.playableCharacters; },
        actorName() { return this.selectedCharacterNames.join(' / ') || this.profile.name || '玩家'; },
        myThreadMessages() {
          if (!this.hasLockedCharacters) return [];
          return this.messages.filter(msg => this.messageBelongsToMe(msg) && ['action', 'ai', 'dice', 'state', 'system'].includes(msg.kind));
        },
        actionThreadPlayers() {
          const threads = new Map();
          for (const msg of this.messages) {
            const actorId = this.messageThreadActorId(msg);
            if (!actorId) continue;
            const payload = msg.payload || {};
            const actorName = payload.actor_name || payload.character_name || msg.sender_name || actorId;
            if (!threads.has(actorId)) threads.set(actorId, { id: actorId, name: actorName, count: 0 });
            const entry = threads.get(actorId);
            entry.count += 1;
            if (payload.character_name) entry.name = payload.character_name;
          }
          return [...threads.values()];
        },
        selectedThreadPlayerId() {
          const ids = new Set(this.actionThreadPlayers.map(player => player.id));
          if (this.activeThreadPlayerId && ids.has(this.activeThreadPlayerId)) return this.activeThreadPlayerId;
          if (ids.has(this.profile.id)) return this.profile.id;
          return this.actionThreadPlayers[0]?.id || '';
        },
        selectedThreadMessages() {
          const selected = this.selectedThreadPlayerId;
          if (!selected) return [];
          return this.messages.filter(msg => this.messageThreadActorId(msg) === selected);
        },
        visibleThreadMessages() {
          return [...this.selectedThreadMessages].reverse();
        },
        tableMessages() {
          return this.messages.filter(msg => msg.kind === 'chat' || this.isPublicTableMessage(msg)).slice(-50);
        },
        visibleTableMessages() {
          return [...this.tableMessages].reverse();
        },
        sceneAiMessages() {
          const currentSceneId = Number(this.gameState?.current_scene_id || this.currentScene?.id || 0);
          return this.messages.filter(msg => {
            if (msg?.kind !== 'ai') return false;
            if (msg.pending || msg.payload?.pending) return false;
            const payload = msg.payload || {};
            const source = payload.source || '';
            if (!['player_action', 'dice', 'scene_advance'].includes(source)) return false;
            const msgSceneId = Number(payload.current_scene_id || payload.scene_id || 0);
            return !currentSceneId || !msgSceneId || msgSceneId === currentSceneId;
          }).slice(-6).reverse();
        },
        maxPlayers() {
          return Number(this.room?.max_players || this.room?.settings?.max_players || this.playableCharacters.length || 1);
        },
        claimedPlayerCount() {
          return Number(this.room?.claimed_character_count ?? this.room?.claimed_player_count ?? 0);
        },
        joinedPlayerCount() {
          return Number(this.room?.joined_player_count ?? this.members.length ?? 0);
        },
        playerSlotsRemaining() {
          const remaining = Number(this.room?.player_slots_remaining);
          if (Number.isFinite(remaining)) return Math.max(0, remaining);
          return Math.max(0, this.maxPlayers - this.joinedPlayerCount);
        },
        characterSlotsRemaining() {
          const remaining = Number(this.room?.character_slots_remaining);
          if (Number.isFinite(remaining)) return Math.max(0, remaining);
          return Math.max(0, this.maxPlayers - this.claimedPlayerCount);
        },
        roomFullForNewPlayer() {
          return !!this.room && !this.hasLockedCharacters && this.characterSlotsRemaining <= 0;
        },
        roomSeatLabel() {
          if (this.hasLockedCharacters) return '你已入座';
          return this.characterSlotsRemaining > 0 ? `可选 ${this.characterSlotsRemaining} 角` : '角色已满';
        },
        roomSeatTone() {
          if (this.hasLockedCharacters) return 'var(--good)';
          return this.characterSlotsRemaining > 0 ? 'var(--good)' : 'var(--bad)';
        },
        claimButtonDisabled() {
          return this.selectedCharacterIds.length !== 1 || this.characterClaimBusy || this.hasLockedCharacters || this.roomFullForNewPlayer || this.selectedCharacterIds.length > this.characterSlotsRemaining;
        },
        claimButtonText() {
          if (this.hasLockedCharacters) return '角色已锁定';
          if (this.characterClaimBusy) return '记录中';
          if (this.roomFullForNewPlayer) return '席位已满';
          if (this.selectedCharacterIds.length > 1) return '每位玩家只能确认 1 个角色';
          if (this.selectedCharacterIds.length > this.characterSlotsRemaining) return `最多确认 ${this.characterSlotsRemaining} 个角色`;
          return this.selectedCharacterIds.length ? '确认扮演 ' + this.selectedCharacterNames.join(' / ') : '选择角色后确认';
        },
        dossierActiveTab() {
          return this.dossierTabs.find(tab => tab.id === this.dossierTab) || this.dossierTabs[0];
        },
        dossierVisibleRagDocuments() {
          const q = this.dossierSearch.toLowerCase();
          return this.ragDocuments
            .filter(doc => !doc.hidden)
            .filter(doc => !q || `${doc.title || ''} ${doc.source || ''}`.toLowerCase().includes(q));
        },
        dossierFilteredLore() {
          const q = this.dossierSearch.toLowerCase();
          return this.lorebook.filter(item => !q || `${item.keywords || ''} ${item.content || ''}`.toLowerCase().includes(q));
        },
        dossierFilteredEntities() {
          const q = this.dossierSearch.toLowerCase();
          return this.worldEntities.filter(entity => !q || `${entity.name || ''} ${entity.location || ''} ${entity.status || ''} ${entity.state_desc || ''}`.toLowerCase().includes(q));
        },
        dossierDocText() {
          return this.dossierDocChunks.map(chunk => chunk.chunk_text).filter(Boolean).join('\n\n');
        },
        mapVisibleRooms() {
          return this.mapRooms.filter(room => (room.floor ?? 1) === this.mapActiveFloor);
        },
        mapVisibleEdges() {
          return this.mapEdges.filter(edge => {
            const fromRoom = this.mapRoomById(edge.from_id);
            const toRoom = this.mapRoomById(edge.to_id);
            if (!fromRoom || !toRoom) return false;
            const fromFloor = fromRoom.floor ?? 1;
            const toFloor = toRoom.floor ?? 1;
            if (fromFloor === toFloor) return fromFloor === this.mapActiveFloor;
            return fromFloor === this.mapActiveFloor || toFloor === this.mapActiveFloor;
          });
        },
        dossierMapViewBox() {
          return `${this.dossierMapViewX} ${this.dossierMapViewY} ${this.dossierMapSvgW / this.dossierMapScale} ${this.dossierMapSvgH / this.dossierMapScale}`;
        },
      },
      async mounted() {
        const params = new URLSearchParams(location.search);
        const urlRoomCode = (params.get('room') || params.get('code') || '').trim().toUpperCase();
        const urlMemberToken = (params.get('member_token') || params.get('token') || '').trim();
        if (urlRoomCode) this.joinCode = urlRoomCode;
        if (urlRoomCode && urlMemberToken) {
          this.memberTokens[urlRoomCode] = urlMemberToken;
          this.saveRoomTokens();
        }
        await this.refreshAccount();
        this.ensureUniqueTabIdentity({ preserveIdentity: Boolean(urlRoomCode) });
        window.addEventListener('beforeunload', this.clearCurrentPresence);
        await this.fetchStatLabels();
        if (urlRoomCode) {
          if (this.account) {
            this.joinRoomByCode(urlRoomCode, '', { preserveIdentity: true }).catch(err => { this.characterClaimMsg = err.message || '加入房间失败'; });
          } else {
            this.authMsg = '请先登录账号后进入房间';
          }
        }
      },
      beforeUnmount() {
        window.removeEventListener('beforeunload', this.clearCurrentPresence);
        this.stopPresence();
      },
      methods: {
        applyAuthSession(data) {
          const account = data?.account;
          if (!account?.player_id) return;
          this.account = account;
          if (data.token) {
            this.accountToken = data.token;
            localStorage.setItem('zric-auth-token', data.token);
          }
          localStorage.setItem('zric-auth-account', JSON.stringify(account));
          this.profile.id = account.player_id;
          this.profile.name = account.display_name || account.username || '玩家';
          this.saveProfile();
        },
        clearAuthSession() {
          this.account = null;
          this.accountToken = '';
          this.memberTokens = {};
          localStorage.removeItem('zric-auth-token');
          localStorage.removeItem('zric-auth-account');
          sessionStorage.removeItem('zric-mp-tab-tokens');
          const stored = readStoredJson(localStorage, 'zric-mp-tokens', {});
          localStorage.setItem('zric-mp-tokens', JSON.stringify({ ...stored, memberTokens: {} }));
          if (this.ws) this.ws.close();
        },
        async refreshAccount() {
          if (!this.accountToken) return;
          try {
            const res = await fetch(`${API_BASE_URL}/api/auth/me`, { headers: { 'X-Auth-Token': this.accountToken } });
            const data = await res.json().catch(() => ({}));
            if (!res.ok || data.status === 'error') throw new Error(data.detail || data.message || '账号状态失效');
            this.applyAuthSession(data);
          } catch (_) {
            this.clearAuthSession();
          }
        },
        async submitAuth() {
          this.authBusy = true;
          this.authMsg = '';
          try {
            const path = this.authMode === 'register' ? '/api/auth/register' : '/api/auth/login';
            const res = await fetch(`${API_BASE_URL}${path}`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify(this.authForm),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok || data.status === 'error') throw new Error(data.detail || data.message || '登录失败');
            this.applyAuthSession(data);
            this.authForm.password = '';
            this.authMsg = '账号已登录';
            if (this.joinCode && !this.room) await this.joinRoomByCode(this.joinCode, '', { preserveIdentity: true });
          } catch (err) {
            this.authMsg = err.message || '登录失败';
          } finally {
            this.authBusy = false;
          }
        },
        async logoutAccount() {
          this.authBusy = true;
          try {
            if (this.accountToken) {
              await fetch(`${API_BASE_URL}/api/auth/logout`, { method: 'POST', headers: { 'X-Auth-Token': this.accountToken } });
            }
          } catch (_) {
          } finally {
            this.clearAuthSession();
            this.room = null;
            this.authMsg = '账号已退出';
            this.authBusy = false;
          }
        },
        requireAccount() {
          if (this.account) return true;
          this.authMsg = '请先登录账号';
          return false;
        },
        saveProfile() {
          this.profile.role = 'player';
          sessionStorage.setItem('zric-mp-tab-profile', JSON.stringify({ ...this.profile, characterIds: this.selectedCharacterIds }));
          localStorage.setItem('zric-mp-client-id', this.clientId);
          localStorage.setItem('zric-mp-profile', JSON.stringify({ id: this.profile.id, name: this.profile.name, role: 'player', color: this.profile.color, client_id: this.clientId }));
        },
        saveRoomTokens() {
          sessionStorage.setItem('zric-mp-tab-tokens', JSON.stringify({ memberTokens: this.memberTokens }));
          const stored = readStoredJson(localStorage, 'zric-mp-tokens', {});
          localStorage.setItem('zric-mp-tokens', JSON.stringify({
            ...stored,
            memberTokens: { ...(stored.memberTokens || {}), ...this.memberTokens },
          }));
        },
        presenceKey(playerId = this.profile.id) {
          return `zric-mp-presence:${playerId}`;
        },
        readPresence(playerId = this.profile.id) {
          return readStoredJson(localStorage, this.presenceKey(playerId), null);
        },
        isReloadNavigation() {
          const nav = performance.getEntriesByType?.('navigation')?.[0];
          return nav?.type === 'reload';
        },
        clearCurrentPresence() {
          const current = this.readPresence();
          if (!current || current.tabInstanceId === this.tabInstanceId) {
            localStorage.removeItem(this.presenceKey());
          }
        },
        stopPresence() {
          if (this.presenceTimer) clearInterval(this.presenceTimer);
          this.presenceTimer = null;
          this.clearCurrentPresence();
        },
        startPresence() {
          if (this.presenceTimer) clearInterval(this.presenceTimer);
          const writePresence = () => {
            localStorage.setItem(this.presenceKey(), JSON.stringify({
              tabInstanceId: this.tabInstanceId,
              updatedAt: Date.now(),
            }));
          };
          writePresence();
          this.presenceTimer = setInterval(writePresence, 3000);
        },
        ensureUniqueTabIdentity(options = {}) {
          if (this.account) {
            this.profile.id = this.account.player_id;
            this.profile.name = this.account.display_name || this.account.username || this.profile.name || '玩家';
            this.saveProfile();
            this.saveRoomTokens();
            this.startPresence();
            return;
          }
          const current = this.readPresence();
          const activeElsewhere = current
            && current.tabInstanceId !== this.tabInstanceId
            && Date.now() - Number(current.updatedAt || 0) < 10000;
          if (activeElsewhere && options.preserveIdentity) {
            this.saveProfile();
            this.saveRoomTokens();
            this.startPresence();
            return;
          }
          if (activeElsewhere && !this.isReloadNavigation()) {
            this.profile.id = crypto.randomUUID();
            this.selectedCharacterIds = [];
            this.memberTokens = {};
            sessionStorage.removeItem('zric-mp-tab-tokens');
            this.characterClaimMsg = '检测到同浏览器已有活跃玩家，已为本标签页生成新身份';
          }
          this.saveProfile();
          this.saveRoomTokens();
          this.startPresence();
        },
        extractRoomCode(path) {
          const match = String(path || '').match(/\/api\/multiplayer\/rooms\/([^/?]+)/);
          return match ? decodeURIComponent(match[1]) : '';
        },
        async api(path, options = {}) {
          const jsonBody = !(options.body instanceof FormData);
          const headers = jsonBody ? {'Content-Type': 'application/json'} : {};
          const roomCode = this.room?.code || this.extractRoomCode(path);
          if (this.accountToken) headers['X-Auth-Token'] = this.accountToken;
          if (roomCode && this.memberTokens[roomCode]) headers['X-Member-Token'] = this.memberTokens[roomCode];
          const res = await fetch(`${API_BASE_URL}${path}`, { ...options, headers: { ...headers, ...(options.headers || {}) } });
          const data = await res.json().catch(() => ({}));
          if (!res.ok || data.status === 'error') throw new Error(data.detail || data.message || '请求失败');
          return data;
        },
        async fetchStatLabels() {
          try {
            const res = await fetch(`${API_BASE_URL}/api/game/stat-labels`);
            const data = await res.json();
            this.hpLabel = data.hp_label || 'HP';
            this.sanLabel = data.san_label || 'SAN';
          } catch (_) {}
        },
        async fetchWorldview() {
          try {
            const res = await fetch(`${API_BASE_URL}/api/game/worldview`);
            const data = await res.json();
            this.worldviewContent = data.content || '';
          } catch (_) {}
        },
        async fetchMemory() {
          try {
            const res = await fetch(`${API_BASE_URL}/api/game/memory`);
            const data = await res.json();
            this.memoryContent = data.content || '';
          } catch (_) {}
        },
        async fetchLorebook() {
          try {
            const res = await fetch(`${API_BASE_URL}/api/game/lorebook`);
            const data = await res.json();
            this.lorebook = data.lorebook || [];
          } catch (_) {}
        },
        async fetchWorldEntities() {
          try {
            const res = await fetch(`${API_BASE_URL}/api/world-entities`);
            const data = await res.json();
            this.worldEntities = data.entities || [];
          } catch (_) {}
        },
        async fetchRagDocuments() {
          try {
            const res = await fetch(`${API_BASE_URL}/api/rag/documents`);
            const data = await res.json();
            this.ragDocuments = data.documents || [];
          } catch (_) {}
        },
        async fetchMapData() {
          try {
            const res = await fetch(`${API_BASE_URL}/api/map/rooms?map_id=1`);
            const data = await res.json();
            this.mapRooms = (data.rooms || []).map(room => ({ ...room, floor: room.floor ?? 1 }));
            this.mapEdges = (data.edges || []).map(edge => ({ ...edge, edge_type: edge.edge_type || 'normal' }));
            this.mapFloors = data.floors || [1];
            if (!this.mapFloors.includes(this.mapActiveFloor)) this.mapActiveFloor = this.mapFloors[0] || 1;
            if (this.currentRoom?.id) {
              const activeRoom = this.mapRooms.find(room => String(room.id) === String(this.currentRoom.id));
              if (activeRoom) this.mapActiveFloor = activeRoom.floor ?? 1;
            }
            if (!this.dossierSelectedRoom || !this.mapRooms.some(room => room.id === this.dossierSelectedRoom.id)) {
              this.dossierSelectedRoom = this.mapVisibleRooms[0] ? { ...this.mapVisibleRooms[0] } : null;
            }
          } catch (_) {}
        },
        async fetchTimelines() {
          try {
            const res = await fetch(`${API_BASE_URL}/api/timelines`);
            const data = await res.json();
            this.timelines = data.timelines || [];
          } catch (_) {}
        },
        async selectDossierDoc(docId) {
          this.dossierSelectedDocId = docId;
          this.dossierSelectedDoc = null;
          this.dossierDocChunks = [];
          if (!docId) return;
          this.dossierLoadingDoc = true;
          try {
            const res = await fetch(`${API_BASE_URL}/api/rag/documents/${docId}`);
            const data = await res.json();
            if (data.status === 'success') {
              this.dossierSelectedDoc = data.document;
              this.dossierDocChunks = data.chunks || [];
            }
          } catch (_) {
          } finally {
            this.dossierLoadingDoc = false;
          }
        },
        async refreshDossierTabData(tab = this.dossierTab) {
          if (tab === 'worldview') await this.fetchWorldview();
          if (tab === 'map') {
            await Promise.all([this.fetchMapData(), this.fetchWorldEntities(), this.fetchTimelines()]);
            this.scheduleDossierMapFit();
          }
          if (tab === 'knowledge') {
            await this.fetchRagDocuments();
            const firstDoc = this.dossierVisibleRagDocuments[0];
            if (!this.dossierSelectedDocId && firstDoc) await this.selectDossierDoc(firstDoc.id);
          }
          if (tab === 'lore') await this.fetchLorebook();
          if (tab === 'entities') await Promise.all([this.fetchWorldEntities(), this.fetchMapData()]);
          if (tab === 'memory') await this.fetchMemory();
        },
        async switchDossierTab(tab) {
          this.dossierTab = tab;
          this.dossierSearch = '';
          await this.refreshDossierTabData(tab);
        },
        async openDossierPanel(tab = 'worldview') {
          this.dossierTab = tab;
          this.dossierSearch = '';
          this.showDossierModal = true;
          await this.refreshDossierTabData(tab);
        },
        dossierTabCount(tabId) {
          if (tabId === 'map') return this.mapRooms.length || '';
          if (tabId === 'knowledge') return this.ragDocuments.filter(doc => !doc.hidden).length || '';
          if (tabId === 'lore') return this.lorebook.length || '';
          if (tabId === 'entities') return this.worldEntities.length || '';
          return '';
        },
        mapRoomById(id) {
          return this.mapRooms.find(room => String(room.id) === String(id)) || null;
        },
        selectDossierFloor(floor) {
          this.mapActiveFloor = floor;
          this.dossierSelectedRoom = null;
          this.scheduleDossierMapFit();
        },
        updateDossierMapSize() {
          const el = this.$refs.dossierMapCanvasRef;
          const rect = el?.getBoundingClientRect?.();
          this.dossierMapSvgW = Math.max(320, rect?.width || el?.clientWidth || this.dossierMapSvgW || 900);
          this.dossierMapSvgH = Math.max(320, rect?.height || el?.clientHeight || this.dossierMapSvgH || 620);
          return Boolean(rect && rect.width > 8 && rect.height > 8);
        },
        scheduleDossierMapFit() {
          nextTick(() => {
            const raf = window.requestAnimationFrame || ((fn) => setTimeout(fn, 16));
            raf(() => {
              if (!this.updateDossierMapSize()) {
                setTimeout(() => this.dossierFitMapToFloor(), 60);
                return;
              }
              this.dossierFitMapToFloor();
            });
          });
        },
        dossierFitMapToFloor() {
          if (!this.updateDossierMapSize()) {
            setTimeout(() => this.dossierFitMapToFloor(), 60);
            return;
          }
          const rooms = this.mapVisibleRooms;
          if (!rooms.length) {
            this.dossierMapScale = 1;
            this.dossierMapViewX = 0;
            this.dossierMapViewY = 0;
            this.dossierSelectedRoom = null;
            return;
          }
          const minX = Math.min(...rooms.map(room => Number(room.x || 0)));
          const minY = Math.min(...rooms.map(room => Number(room.y || 0)));
          const maxX = Math.max(...rooms.map(room => Number(room.x || 0) + Number(room.w || 160)));
          const maxY = Math.max(...rooms.map(room => Number(room.y || 0) + Number(room.h || 96)));
          const pad = 90;
          const boundsW = Math.max(1, maxX - minX);
          const boundsH = Math.max(1, maxY - minY);
          const scale = Math.min(
            this.dossierMapSvgW / (boundsW + pad * 2),
            this.dossierMapSvgH / (boundsH + pad * 2)
          );
          this.dossierMapScale = Math.min(2.2, Math.max(0.25, scale));
          const viewW = this.dossierMapSvgW / this.dossierMapScale;
          const viewH = this.dossierMapSvgH / this.dossierMapScale;
          this.dossierMapViewX = minX + boundsW / 2 - viewW / 2;
          this.dossierMapViewY = minY + boundsH / 2 - viewH / 2;
        },
        dossierSelectMapRoom(room, fromList = false) {
          if (!fromList && this.dossierMapSuppressClick) return;
          this.dossierSelectedRoom = room ? { ...room } : null;
          this.dossierSelectedEntity = null;
        },
        dossierMapEventPoint(event) {
          if (event?.touches?.length) return event.touches[0];
          if (event?.changedTouches?.length) return event.changedTouches[0];
          return event || { clientX: 0, clientY: 0 };
        },
        dossierMapClientToSvg(event) {
          const point = this.dossierMapEventPoint(event);
          const rect = this.$refs.dossierMapCanvasRef?.getBoundingClientRect?.();
          if (!rect) return { x: 0, y: 0 };
          return {
            x: (point.clientX - rect.left) / this.dossierMapScale + this.dossierMapViewX,
            y: (point.clientY - rect.top) / this.dossierMapScale + this.dossierMapViewY,
          };
        },
        dossierMapOnWheel(event) {
          this.updateDossierMapSize();
          const before = this.dossierMapClientToSvg(event);
          const nextScale = Math.min(3, Math.max(0.2, this.dossierMapScale * (event.deltaY > 0 ? 0.9 : 1.1)));
          this.dossierMapScale = nextScale;
          const rect = this.$refs.dossierMapCanvasRef?.getBoundingClientRect?.();
          if (rect) {
            this.dossierMapViewX = before.x - (event.clientX - rect.left) / nextScale;
            this.dossierMapViewY = before.y - (event.clientY - rect.top) / nextScale;
          }
        },
        dossierMapStartPan(event) {
          if (event.button !== undefined && event.button !== 0) return;
          const point = this.dossierMapEventPoint(event);
          this.updateDossierMapSize();
          this.dossierMapPanning = true;
          this.dossierMapSuppressClick = false;
          this.dossierMapPanStart = {
            x: point.clientX,
            y: point.clientY,
            vx: this.dossierMapViewX,
            vy: this.dossierMapViewY,
            dragged: false,
          };
        },
        dossierMapMovePan(event) {
          if (!this.dossierMapPanning) return;
          const point = this.dossierMapEventPoint(event);
          const dx = point.clientX - this.dossierMapPanStart.x;
          const dy = point.clientY - this.dossierMapPanStart.y;
          this.dossierMapPanStart.dragged = this.dossierMapPanStart.dragged || Math.hypot(dx, dy) > 4;
          this.dossierMapViewX = this.dossierMapPanStart.vx - dx / this.dossierMapScale;
          this.dossierMapViewY = this.dossierMapPanStart.vy - dy / this.dossierMapScale;
        },
        dossierMapEndPan() {
          if (this.dossierMapPanStart.dragged) {
            this.dossierMapSuppressClick = true;
            setTimeout(() => { this.dossierMapSuppressClick = false; }, 80);
          }
          this.dossierMapPanning = false;
        },
        dossierMapOnMouseDown(event) {
          this.dossierMapStartPan(event);
        },
        dossierMapOnMouseMove(event) {
          this.dossierMapMovePan(event);
        },
        dossierMapOnMouseUp() {
          this.dossierMapEndPan();
        },
        dossierMapOnTouchStart(event) {
          this.dossierMapStartPan(event);
        },
        dossierMapOnTouchMove(event) {
          this.dossierMapMovePan(event);
        },
        dossierMapOnTouchEnd() {
          this.dossierMapEndPan();
        },
        async joinRoomByCode(code, roleOverride = '', options = {}) {
          const roomCode = String(code || '').trim().toUpperCase();
          if (!roomCode) return;
          if (!this.requireAccount()) return;
          this.joinBusy = true;
          this.characterClaimMsg = '';
          this.ensureUniqueTabIdentity(options);
          try {
            const data = await this.api(`/api/multiplayer/rooms/${encodeURIComponent(roomCode)}/join`, {
              method: 'POST',
              body: JSON.stringify({
                player_id: this.account?.player_id || this.profile.id,
                display_name: this.account?.display_name || this.profile.name,
                role: roleOverride || 'player',
                color: this.profile.color,
                client_id: this.clientId,
              }),
            });
            this.syncJoinedMember(data);
            if (data.room?.code && data.member_token) {
              this.memberTokens[data.room.code] = data.member_token;
              this.saveRoomTokens();
            }
            this.applySnapshot(data);
            this.connectWs();
          } finally {
            this.joinBusy = false;
          }
        },
        async resetPlayerIdentity() {
          const code = (this.room?.code || this.joinCode || '').trim().toUpperCase();
          if (this.ws) this.ws.close();
          this.ws = null;
          this.wsConnected = false;
          this.characterClaimMsg = '正在恢复当前玩家身份...';
          if (code) {
            try {
              await this.joinRoomByCode(code, '', { preserveIdentity: true });
              this.characterClaimMsg = '已恢复当前玩家身份';
            } catch (err) {
              this.characterClaimMsg = err.message || '重新连接失败';
            }
          }
        },
        syncJoinedMember(data) {
          const member = data?.member;
          if (!member?.player_id) return;
          this.profile.id = member.player_id;
          this.profile.name = member.display_name || this.profile.name || '玩家';
          this.profile.color = member.color || this.profile.color || '#7dd3fc';
          this.saveProfile();
        },
        applySnapshot(data) {
          this.syncJoinedMember(data);
          this.room = data.room || this.room;
          this.gameState = data.game_state || this.gameState || {};
          this.members = data.members || this.members;
          this.messages = data.messages || this.messages;
          this.characterClaims = data.character_claims || this.gameState.character_claims || this.characterClaims || [];
          this.checkpointCount = Number(data.checkpoint_count ?? this.checkpointCount ?? 0);
          this.joinCode = this.room ? this.room.code : this.joinCode;
          if (this.hasLockedCharacters) this.selectedCharacterIds = [...this.lockedCharacterIds];
          this.pruneSelectedCharacters();
          this.syncBgmElement();
          this.scrollMessages();
        },
        syncBgmElement() {
          nextTick(() => {
            const el = this.$refs.audioEl;
            if (!el) return;
            el.volume = Number(this.bgmVolume || 0.3);
            if (this.bgmPlaying && this.bgmUrl) this.playBgm();
          });
        },
        async playBgm() {
          await nextTick();
          const el = this.$refs.audioEl;
          if (!el || !this.bgmUrl) {
            this.bgmPlaying = false;
            return false;
          }
          el.volume = Number(this.bgmVolume || 0.3);
          try {
            await el.play();
            this.bgmPlaying = true;
            return true;
          } catch (_) {
            this.bgmPlaying = false;
            return false;
          }
        },
        async toggleBgm() {
          const el = this.$refs.audioEl;
          if (!el || !this.bgmUrl) return;
          if (this.bgmPlaying) {
            el.pause();
            this.bgmPlaying = false;
          } else {
            await this.playBgm();
          }
        },
        async rollbackTurn() {
          if (!this.room || this.rollbackBusy || this.checkpointCount <= 0) return;
          this.rollbackBusy = true;
          this.characterClaimMsg = '';
          try {
            const data = await this.api(`/api/multiplayer/rooms/${this.room.code}/rollback`, {
              method: 'POST',
              body: JSON.stringify({ player_id: this.profile.id }),
            });
            this.applySnapshot(data);
          } catch (err) {
            this.characterClaimMsg = err.message || '返回上一回合失败';
          } finally {
            this.rollbackBusy = false;
          }
        },
        pruneSelectedCharacters() {
          const base = this.hasLockedCharacters ? this.allCharacters : this.playableCharacters;
          if (!base.length) return;
          const ids = new Set(base.map(char => String(char.id)));
          this.selectedCharacterIds = this.selectedCharacterIds.filter(id => ids.has(String(id)));
          if (!this.hasLockedCharacters && this.selectedCharacterIds.length > 1) {
            this.selectedCharacterIds = this.selectedCharacterIds.slice(0, 1);
          }
        },
        connectWs() {
          if (!this.room || !this.accountToken || !window.location.protocol.startsWith('http')) return;
          if (this.ws) this.ws.close();
          const params = new URLSearchParams({ player_id: this.profile.id, name: this.profile.name, role: 'player', client_id: this.clientId, auth_token: this.accountToken });
          if (this.memberTokens[this.room.code]) params.set('member_token', this.memberTokens[this.room.code]);
          const wsUrl = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/rooms/${this.room.code}?${params}`;
          this.ws = new WebSocket(wsUrl);
          this.ws.onopen = () => { this.wsConnected = true; };
          this.ws.onclose = () => { this.wsConnected = false; };
          this.ws.onerror = () => { this.wsConnected = false; };
          this.ws.onmessage = event => this.handleEvent(JSON.parse(event.data));
        },
        actionTextFromMessage(message) {
          return String(message?.payload?.action || message?.content || '').trim();
        },
        isPlayerActionMessage(message) {
          return message?.kind === 'action' && message?.payload?.source === 'player_action';
        },
        isAiAdjudicationMessage(message) {
          return message?.kind === 'ai' && message?.payload?.source === 'player_action';
        },
        isPendingAdjudicationFor(message, actorId, action = '') {
          const payload = message?.payload || {};
          if (payload.source !== 'adjudication_pending') return false;
          if (String(payload.actor_id || '') !== String(actorId || '')) return false;
          return !action || String(payload.action || '').trim() === String(action || '').trim();
        },
        hasAiAdjudicationFor(actorId, action) {
          return this.messages.some(message => {
            if (!this.isAiAdjudicationMessage(message)) return false;
            return String(message.payload?.actor_id || '') === String(actorId || '')
              && this.actionTextFromMessage(message) === String(action || '').trim();
          });
        },
        removeMatchingLocalAction(message) {
          const actorId = String(message?.payload?.actor_id || message?.sender_id || '');
          const action = this.actionTextFromMessage(message);
          if (!actorId || !action) return;
          this.messages = this.messages.filter(item => {
            const payload = item.payload || {};
            if (!payload.local || item.kind !== 'action') return true;
            return String(payload.actor_id || item.sender_id || '') !== actorId || this.actionTextFromMessage(item) !== action;
          });
        },
        ensurePendingAdjudicationForAction(message) {
          const actorId = String(message?.payload?.actor_id || message?.sender_id || '');
          const actorName = message?.payload?.actor_name || message?.payload?.character_name || message?.sender_name || '玩家';
          const action = this.actionTextFromMessage(message);
          if (!actorId || !action || this.hasAiAdjudicationFor(actorId, action)) return;
          if (this.messages.some(item => this.isPendingAdjudicationFor(item, actorId, action))) return;
          const suffix = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
          this.messages.push({
            id: `local-adjudication-${suffix}`,
            sender_id: 'ai-kp',
            sender_name: 'AI-GM',
            kind: 'ai',
            content: `AI-GM 正在裁决「${action.slice(0, 80)}」...`,
            created_at: new Date().toISOString(),
            pending: true,
            payload: {
              source: 'adjudication_pending',
              pending: true,
              actor_id: actorId,
              actor_name: actorName,
              action,
            },
          });
        },
        clearPendingAdjudicationFor(message) {
          const actorId = String(message?.payload?.actor_id || '');
          const action = this.actionTextFromMessage(message);
          if (!actorId) return;
          this.messages = this.messages.filter(item => !this.isPendingAdjudicationFor(item, actorId, action));
        },
        appendThreadMessage(message) {
          if (!message?.id) return;
          if (this.isPlayerActionMessage(message)) this.removeMatchingLocalAction(message);
          if (!this.messages.some(item => item.id === message.id)) this.messages.push(message);
          if (this.isPlayerActionMessage(message)) this.ensurePendingAdjudicationForAction(message);
          if (this.isAiAdjudicationMessage(message)) this.clearPendingAdjudicationFor(message);
          this.scrollMessages();
        },
        addOptimisticActionMessage(action, payload) {
          const actorId = String(this.profile.id || '');
          if (!actorId) return;
          const suffix = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
          const selected = this.selectedCharacters;
          const localPayload = {
            source: 'player_action',
            local: true,
            actor_id: actorId,
            actor_name: this.actorName,
            action,
            action_type: payload.action_type,
            context: payload.context,
            character_ids: selected.map(char => char.id),
          };
          if (selected.length === 1) {
            localPayload.character_id = selected[0].id;
            localPayload.character_name = selected[0].name;
          }
          const message = {
            id: `local-action-${suffix}`,
            sender_id: actorId,
            sender_name: this.actorName,
            kind: 'action',
            content: action,
            created_at: new Date().toISOString(),
            payload: localPayload,
          };
          this.messages.push(message);
          this.activeThreadPlayerId = actorId;
          this.ensurePendingAdjudicationForAction(message);
          this.scrollMessages();
        },
        handleEvent(evt) {
          if (evt.member_token && evt.member) this.syncJoinedMember(evt);
          if (evt.member_token && evt.room?.code) {
            this.memberTokens[evt.room.code] = evt.member_token;
            this.saveRoomTokens();
          }
          if (evt.type === 'snapshot') this.applySnapshot(evt);
          else if (evt.type === 'message.created') {
            this.appendThreadMessage(evt.message);
          } else if (evt.type === 'game.state') {
            this.gameState = evt.game_state || this.gameState || {};
            this.characterClaims = this.gameState.character_claims || this.characterClaims || [];
            if (this.hasLockedCharacters) this.selectedCharacterIds = [...this.lockedCharacterIds];
            this.pruneSelectedCharacters();
            this.syncBgmElement();
          } else if (evt.type === 'member.updated') {
            const index = this.members.findIndex(m => m.player_id === evt.member.player_id);
            if (index >= 0) this.members.splice(index, 1, evt.member);
            else this.members.push(evt.member);
          } else if (evt.type === 'member.left') {
            const member = this.members.find(m => m.player_id === evt.player_id);
            if (member) member.connected = 0;
          } else if (evt.type === 'room.updated') {
            this.applySnapshot(evt.snapshot);
          }
        },
        toggleCharacter(id) {
          const idKey = String(id);
          const char = this.playableCharacters.find(item => String(item.id) === idKey);
          if (!char || this.isCharacterDisabled(char)) return;
          if (this.isCharacterSelected(id)) {
            this.selectedCharacterIds = this.selectedCharacterIds.filter(x => String(x) !== idKey);
            this.characterClaimMsg = '';
            return;
          }
          if (this.selectedCharacterIds.length >= this.characterSlotsRemaining) {
            this.characterClaimMsg = `本房间还可确认 ${this.characterSlotsRemaining} 个角色席位`;
            return;
          }
          this.selectedCharacterIds = [id];
          this.characterClaimMsg = '';
        },
        isCharacterSelected(id) {
          const idKey = String(id);
          return this.selectedCharacterIds.some(selectedId => String(selectedId) === idKey);
        },
        clearCharacters() {
          if (this.hasLockedCharacters) return;
          this.selectedCharacterIds = [];
          this.characterClaimMsg = '';
        },
        async claimCharacters() {
          if (!this.room || !this.selectedCharacterIds.length || this.hasLockedCharacters || this.roomFullForNewPlayer) return;
          if (this.selectedCharacterIds.length !== 1) {
            this.characterClaimMsg = '每位玩家只能确认 1 个角色';
            return;
          }
          if (this.selectedCharacterIds.length > this.characterSlotsRemaining) {
            this.characterClaimMsg = `最多确认 ${this.characterSlotsRemaining} 个角色`;
            return;
          }
          this.characterClaimBusy = true;
          this.characterClaimMsg = '';
          try {
            const data = await this.api(`/api/multiplayer/rooms/${this.room.code}/characters/claim`, {
              method: 'POST',
              body: JSON.stringify({ player_id: this.profile.id, character_ids: this.selectedCharacterIds }),
            });
            this.applySnapshot(data);
            this.characterClaimMsg = '角色已确认';
          } catch (err) {
            this.characterClaimMsg = err.message || '角色确认失败';
          } finally {
            this.characterClaimBusy = false;
          }
        },
        claimForCharacter(char) {
          const charId = String(char?.id ?? '');
          return this.characterClaims.find(claim => String(claim.character_id) === charId) || null;
        },
        isOwnClaim(char) {
          const claim = this.claimForCharacter(char);
          return !!claim && String(claim.player_id || '') === String(this.profile.id || '');
        },
        isClaimedByOther(char) {
          const claim = this.claimForCharacter(char);
          return !!claim && String(claim.player_id || '') !== String(this.profile.id || '');
        },
        isCharacterDisabled(char) {
          if (this.isClaimedByOther(char) || this.roomFullForNewPlayer || (this.hasLockedCharacters && !this.isOwnClaim(char))) return true;
          return !this.isCharacterSelected(char?.id) && this.selectedCharacterIds.length >= this.characterSlotsRemaining;
        },
        characterClaimLabel(char) {
          if (this.isOwnClaim(char)) return '已锁定';
          const claim = this.claimForCharacter(char);
          return claim ? '已占用' : '';
        },
        characterClaimLine(char) {
          const claim = this.claimForCharacter(char);
          if (!claim) return '';
          return String(claim.player_id || '') === String(this.profile.id || '') ? '你已确认扮演该角色' : `已由 ${claim.display_name || '其他玩家'} 扮演`;
        },
        memberClaimName(playerId) {
          const claim = this.characterClaims.find(item => String(item.player_id || '') === String(playerId || ''));
          return claim ? (claim.character_name || claim.display_name || '') : '';
        },
        sceneMessageActorName(msg) {
          const payload = msg?.payload || {};
          const actor = payload.character_name || payload.actor_name || '';
          return actor ? `AI-GM · ${actor}` : (msg?.sender_name || 'AI-GM');
        },
        messageBelongsToMe(msg) {
          const payload = msg?.payload || {};
          const playerId = String(this.profile.id || '');
          const ownCharacterIds = new Set(this.lockedCharacterIds.map(id => String(id)));
          const actorId = String(payload.actor_id || msg?.sender_id || '');
          const characterId = String(payload.character_id || '');
          if (actorId && actorId === playerId) return true;
          if (characterId && ownCharacterIds.has(characterId)) return true;
          if (Array.isArray(payload.character_ids) && payload.character_ids.some(id => ownCharacterIds.has(String(id)))) return true;
          return false;
        },
        messageThreadActorId(msg) {
          const payload = msg?.payload || {};
          const source = payload.source || '';
          const kind = msg?.kind || '';
          if (!['player_action', 'adjudication_pending', 'dice', 'character_updates', 'scene_advance'].includes(source) && !['action', 'dice'].includes(kind)) {
            return '';
          }
          const actorId = String(payload.actor_id || (['action', 'dice'].includes(kind) ? msg?.sender_id : '') || '');
          return actorId && actorId !== 'ai-kp' ? actorId : '';
        },
        isPublicTableMessage(msg) {
          const payload = msg?.payload || {};
          if (payload.actor_id || payload.character_id || Array.isArray(payload.character_ids)) return false;
          return ['system', 'state'].includes(msg?.kind);
        },
        compactText(value, max = 220) {
          return String(value || '').replace(/\s+/g, ' ').trim().slice(0, max);
        },
        characterSeed(char, extra = '') {
          const source = `${char?.id || ''}|${char?.name || ''}|${char?.role || ''}|${char?.personality || ''}|${char?.inventory || ''}|${extra}`;
          return Array.from(source).reduce((sum, ch, idx) => sum + ch.charCodeAt(0) * (idx + 1), 0);
        },
        characterAngleLine(char, extra = '') {
          const name = char?.name || '角色';
          const role = char?.role || '角色';
          const hooks = [
            `${name}会先用「${role}」的专业/本能判断眼前风险。`,
            `${name}更在意哪些细节会影响自己的秘密、目标或安全。`,
            `${name}会从同伴的反应里寻找可以利用或必须警惕的信号。`,
            `${name}会优先确认退路、资源，以及自己还能掌控的东西。`,
            `${name}会把现场异常和自身经历联系起来，而不是只看表面。`,
          ];
          return hooks[this.characterSeed(char, extra) % hooks.length];
        },
        localCharacterOpeningText() {
          const char = this.selectedCharacters[0];
          if (!char) return '';
          const scene = this.currentScene || {};
          const sceneName = scene.name || '开场';
          const publicText = this.publicSceneText || '主持端尚未写下更多场景细节，你先从自己的视角观察当下。';
          const details = [`${char.name || '角色'}，你以「${char.role || '角色'}」的身份进入「${sceneName}」。`];
          if (char.personality) details.push(`你的性格/背景提示是：${this.compactText(char.personality, 160)}。`);
          if (char.inventory) details.push(`你当前随身/状态记录：${this.compactText(char.inventory, 160)}。`);
          details.push(this.characterAngleLine(char, sceneName));
          details.push(`从你的视角看，开场是这样的：${this.compactText(publicText, 420)}`);
          details.push('你可以先用自己的角色口吻描述反应，或直接提交一次行动交给 AI-GM 单独裁定。');
          return details.join('\n');
        },
        characterPerspectiveOptions(char, options, sceneText = '') {
          const list = Array.isArray(options) ? options : [];
          if (!char) return list.map(opt => ({ ...opt, visible_text: opt.text, action_text: opt.text }));
          return list.map((opt, idx) => {
            const rawText = this.compactText(opt?.text, 180) || '自由行动';
            const role = char.role || '角色';
            const name = char.name || '角色';
            const lenses = [
              `以${role}的判断，先${rawText}`,
              `从${name}自己的处境出发，${rawText}`,
              `带着${role}的顾虑，尝试${rawText}`,
              `优先确认这对${name}意味着什么，再${rawText}`,
              `以${name}的个人目标为准，把现场线索和${role}判断联系起来，${rawText}`,
            ];
            const visibleText = lenses[(this.characterSeed(char, `${idx}|${rawText}|${sceneText}`) + idx) % lenses.length];
            return {
              ...opt,
              visible_text: visibleText,
              action_text: `${name}（${role}）：${visibleText}`,
            };
          });
        },
        useOptionAsAction(option) {
          if (!this.hasLockedCharacters) return;
          if (typeof option === 'string') {
            this.actionText = option || this.actionText;
            this.pendingOption = null;
            return;
          }
          const actionText = this.optionActionText(option);
          this.actionText = actionText || this.actionText;
          this.pendingOption = actionText ? { ...option, action_text: actionText } : null;
        },
        optionActionText(option) {
          return option?.action_text || option?.visible_text || option?.text || '';
        },
        pendingOptionPayload(action) {
          const pending = this.pendingOption;
          const pendingAction = String(pending?.action_text || '').trim();
          if (!pending || !pendingAction || String(action || '').trim() !== pendingAction) return {};
          const optionId = Number(pending.id);
          const nextNodeId = Number(pending.next_node_id);
          return {
            option_id: Number.isFinite(optionId) ? optionId : null,
            next_node_id: Number.isFinite(nextNodeId) ? nextNodeId : null,
            option_text: String(pending.text || pending.visible_text || pendingAction).slice(0, 300),
          };
        },
        async sendChat() {
          const content = this.publicChatText.trim();
          if (!content || !this.room || !this.hasLockedCharacters) return;
          if (/^[./!！。]?r(oll)?\s*\d/i.test(content)) {
            await this.rollDice(content);
            this.publicChatText = '';
            this.pendingOption = null;
            return;
          }
          const data = await this.api(`/api/multiplayer/rooms/${this.room.code}/messages`, {
            method: 'POST',
            body: JSON.stringify({ sender_id: this.profile.id, sender_name: this.actorName, content }),
          });
          if (data.message && !this.messages.some(msg => msg.id === data.message.id)) this.messages.push(data.message);
          this.scrollMessages();
          this.publicChatText = '';
          this.pendingOption = null;
        },
        async submitAction() {
          const action = this.actionText.trim();
          if (!action || !this.room || !this.hasLockedCharacters || this.actionBusy) return;
          this.actionBusy = true;
          const payload = {
            action,
            action_type: this.actionType || 'mixed',
            context: this.sceneText || '',
            actor_name: this.actorName,
            ...this.pendingOptionPayload(action),
          };
          this.addOptimisticActionMessage(action, payload);
          try {
            if (this.wsConnected) this.ws.send(JSON.stringify({ type: 'player.action', payload }));
            else {
              const data = await this.api(`/api/multiplayer/rooms/${this.room.code}/action`, {
                method: 'POST',
                body: JSON.stringify({ actor_id: this.profile.id, actor_name: this.actorName, ...payload }),
              });
              [data.message, data.ai_message, data.state_message, data.scene_message].filter(Boolean).forEach(message => {
                this.appendThreadMessage(message);
              });
              if (data.game_state) this.gameState = data.game_state;
            }
            this.actionText = '';
            this.pendingOption = null;
          } catch (err) {
            this.clearPendingAdjudicationFor({ payload: { actor_id: this.profile.id, action } });
            this.characterClaimMsg = err.message || '行动提交失败';
          } finally {
            this.actionBusy = false;
          }
        },
        async rollDice(rawExpression) {
          if (!this.room || !this.hasLockedCharacters) return;
          const expression = String(rawExpression || '1d100').replace(/^[./!！。]?r(oll)?\s*/i, '').trim() || '1d100';
          const payload = { expression, actor_id: this.profile.id, actor_name: this.actorName, context: this.sceneText || '', ask_ai: true };
          if (this.wsConnected) this.ws.send(JSON.stringify({ type: 'dice.roll', payload }));
          else await this.api(`/api/multiplayer/rooms/${this.room.code}/dice`, { method: 'POST', body: JSON.stringify(payload) });
        },
        statusLabel(status) { return ({ active: '在场', hidden: '未登场', benched: '暂离', dead: '死亡' })[status || 'active'] || status || '在场'; },
        messageKindLabel(kind) { return ({ action: '行动', ai: 'AI-GM', dice: '骰子', state: '状态', system: '系统', chat: '公屏' })[kind] || kind || '消息'; },
        shortTime(value) {
          const text = String(value || '');
          const match = text.match(/(\d{2}:\d{2})(?::\d{2})?/);
          return match ? match[1] : text;
        },
        scrollMessages() {
          nextTick(() => {
            const el = this.$refs.messagesEl;
            if (el) el.scrollTop = 0;
          });
        },
      }
    }).mount('#app');
