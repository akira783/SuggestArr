<template>
  <teleport to="body">
    <transition name="modal-fade">
      <div v-if="open" class="modal-overlay" @click.self="$emit('close')">
        <div class="modal swipe-likes-modal" role="dialog" aria-modal="true" aria-labelledby="swipe-likes-title">
          <div class="modal-header">
            <h3 id="swipe-likes-title" class="modal-title"><i aria-hidden="true" class="fas fa-heart"></i> My likes</h3>
            <button type="button" class="modal-close" aria-label="Close" @click="$emit('close')">
              <i aria-hidden="true" class="fas fa-times"></i>
            </button>
          </div>

          <div class="swipe-likes-tabs" role="tablist">
            <button
              v-for="tab in tabs"
              :key="tab.id"
              type="button"
              role="tab"
              class="swipe-segment"
              :class="{ active: activeTab === tab.id }"
              :aria-selected="activeTab === tab.id"
              @click="activeTab = tab.id"
            >
              {{ tab.label }} <span class="swipe-likes-count">{{ lists[tab.id].length }}</span>
            </button>
          </div>

          <div class="modal-body">
            <div v-if="loading" class="swipe-profile-loading"><i aria-hidden="true" class="fas fa-spinner fa-spin"></i> Loading…</div>
            <p v-else-if="!items.length" class="swipe-profile-empty">
              {{ activeTab === 'pending' ? 'Nothing waiting: every card you liked has been requested.' : 'No requested likes yet.' }}
            </p>
            <ul v-else class="swipe-likes-list">
              <li v-for="item in items" :key="key(item)" class="swipe-likes-item">
                <img v-if="item.poster_path" :src="item.poster_path" :alt="item.title" class="swipe-likes-poster" loading="lazy" />
                <div v-else class="swipe-likes-poster swipe-likes-poster-empty">
                  <i aria-hidden="true" :class="item.media_type === 'tv' ? 'fas fa-tv' : 'fas fa-film'"></i>
                </div>
                <div class="swipe-likes-info">
                  <strong>{{ item.title }}</strong>
                  <span class="swipe-likes-meta">
                    {{ item.media_type === 'tv' ? 'Series' : 'Movie' }}<template v-if="item.year"> · {{ item.year }}</template>
                  </span>
                  <span v-if="item.rationale" class="swipe-likes-rationale">{{ item.rationale }}</span>
                </div>
                <button
                  v-if="activeTab === 'pending'"
                  type="button"
                  class="btn btn-primary btn-sm"
                  :disabled="requesting.has(key(item))"
                  @click="request(item)"
                >
                  <i aria-hidden="true" :class="requesting.has(key(item)) ? 'fas fa-spinner fa-spin' : 'fas fa-paper-plane'"></i> Request
                </button>
                <span v-else class="swipe-likes-done"><i aria-hidden="true" class="fas fa-check"></i></span>
              </li>
            </ul>
          </div>
        </div>
      </div>
    </transition>
  </teleport>
</template>

<script>
import { swipeLikes, swipeRequest } from '@/api/swipeApi.js';
import { requestMessage } from '@/utils/swipeDeck.js';

export default {
  name: 'SwipeLikesPanel',

  props: {
    open: { type: Boolean, default: false },
  },

  emits: ['close'],

  data() {
    return {
      loading: false,
      activeTab: 'pending',
      tabs: [
        { id: 'pending', label: 'To request' },
        { id: 'requested', label: 'Requested' },
      ],
      lists: { pending: [], requested: [] },
      requesting: new Set(),
    };
  },

  computed: {
    items() {
      return this.lists[this.activeTab];
    },
  },

  watch: {
    open(value) {
      if (value) this.load();
    },
  },

  methods: {
    key(item) {
      return `${item.media_type}-${item.id}`;
    },

    async load() {
      this.loading = true;
      try {
        const [pending, requested] = await Promise.all([swipeLikes(false), swipeLikes(true)]);
        this.lists = { pending: pending.data.likes, requested: requested.data.likes };
      } catch {
        this.$toast.error('Could not load your likes.');
      } finally {
        this.loading = false;
      }
    },

    async request(item) {
      const key = this.key(item);
      this.requesting = new Set(this.requesting).add(key);
      try {
        const { data } = await swipeRequest(item);
        this.$toast.success(requestMessage(data.request_status, item.title));
        this.lists = {
          pending: this.lists.pending.filter(other => this.key(other) !== key),
          requested: [item, ...this.lists.requested],
        };
      } catch (error) {
        this.$toast.error(error?.response?.data?.message || `Could not request ${item.title}.`);
      } finally {
        const next = new Set(this.requesting);
        next.delete(key);
        this.requesting = next;
      }
    },
  },
};
</script>
