<template>
  <div class="swipe-page">
    <!-- AI provider missing -->
    <div v-if="status && !status.llm_configured" class="empty-state">
      <i class="fas fa-robot"></i>
      <h3>Swipe needs an AI provider</h3>
      <p>Configure an OpenAI-compatible provider in the Advanced settings to get personalised cards.</p>
    </div>

    <template v-else>
      <!-- Controls -->
      <div class="swipe-controls">
        <div class="swipe-segmented" role="group" aria-label="Media type">
          <button
            v-for="option in mediaOptions"
            :key="option.value"
            type="button"
            class="swipe-segment"
            :class="{ active: mediaType === option.value }"
            :aria-pressed="mediaType === option.value"
            @click="setMediaType(option.value)"
          >
            <i :class="option.icon"></i> {{ option.label }}
          </button>
        </div>
        <form class="swipe-mood" @submit.prevent="applyMood">
          <i class="fas fa-lightbulb"></i>
          <input
            v-model="moodInput"
            type="text"
            maxlength="200"
            placeholder="In the mood for… (optional)"
            aria-label="What are you in the mood for?"
          />
          <button v-if="mood" type="button" class="swipe-mood-clear" aria-label="Clear mood" @click="clearMood">
            <i class="fas fa-times"></i>
          </button>
        </form>
        <button type="button" class="btn btn-secondary btn-sm swipe-taste-btn" @click="profileOpen = true">
          <i class="fas fa-user-astronaut"></i> My taste
        </button>
      </div>

      <!-- Calibration banner -->
      <div v-if="inCalibration" class="swipe-calibration">
        <div class="swipe-calibration-text">
          <strong>Calibration {{ calibrationView.done }}/{{ calibrationView.target }}</strong>
          <span>Well-known titles to learn your taste fast. Use <em>Already seen</em> for the ones you know.</span>
        </div>
        <div class="swipe-progress" role="progressbar" :aria-valuenow="calibrationView.done"
             aria-valuemin="0" :aria-valuemax="calibrationView.target">
          <div class="swipe-progress-bar" :style="{ width: calibrationPercent + '%' }"></div>
        </div>
        <button type="button" class="btn btn-ghost btn-sm" @click="skipCalibration">Skip</button>
      </div>

      <!-- Deck -->
      <div class="swipe-stage">
        <div v-if="currentCard" class="swipe-deck">
          <div v-if="nextCard" class="swipe-card swipe-card-next" aria-hidden="true">
            <img v-if="nextCard.backdrop_path || nextCard.poster_path"
                 :src="nextCard.backdrop_path || nextCard.poster_path" alt="" class="swipe-card-image" />
          </div>

          <article
            :key="currentKey"
            ref="card"
            class="swipe-card"
            :class="{ dragging: drag.active, leaving: leaving }"
            :style="cardStyle"
            @pointerdown="onPointerDown"
            @pointermove="onPointerMove"
            @pointerup="onPointerUp"
            @pointercancel="onPointerCancel"
          >
            <div class="swipe-stamp swipe-stamp-like" :style="{ opacity: stampOpacity('like') }">LIKE</div>
            <div class="swipe-stamp swipe-stamp-nope" :style="{ opacity: stampOpacity('dislike') }">NOPE</div>

            <div class="swipe-card-media">
              <img v-if="currentCard.backdrop_path || currentCard.poster_path"
                   :src="currentCard.backdrop_path || currentCard.poster_path"
                   :alt="title(currentCard)" class="swipe-card-image" draggable="false" />
              <div v-else class="swipe-card-placeholder">
                <i :class="currentCard.media_type === 'tv' ? 'fas fa-tv' : 'fas fa-film'"></i>
              </div>
              <div class="swipe-card-badges">
                <span class="swipe-badge">
                  <i :class="currentCard.media_type === 'tv' ? 'fas fa-tv' : 'fas fa-film'"></i>
                  {{ currentCard.media_type === 'tv' ? 'Series' : 'Movie' }}
                </span>
                <span v-if="pick(currentCard)" class="swipe-badge swipe-badge-pick"
                      :class="'pick-' + currentCard.pick_type">
                  <i :class="currentCard.pick_type === 'explore' ? 'fas fa-dice' : 'fas fa-compass'"></i>
                  {{ pick(currentCard) }}
                </span>
              </div>
            </div>

            <div class="swipe-card-body">
              <h2 class="swipe-card-title">
                {{ title(currentCard) }}
                <span v-if="year(currentCard)" class="swipe-card-year">{{ year(currentCard) }}</span>
              </h2>

              <div class="swipe-card-meta">
                <span v-if="currentCard.rating" class="swipe-meta" title="TMDb rating">
                  <i class="fas fa-star"></i> {{ Number(currentCard.rating).toFixed(1) }}
                </span>
                <span v-if="currentCard.ratings && currentCard.ratings.imdb_rating" class="swipe-meta" title="IMDb rating">
                  IMDb {{ currentCard.ratings.imdb_rating }}
                </span>
                <span v-if="currentCard.ratings && currentCard.ratings.rotten_tomatoes != null" class="swipe-meta"
                      title="Rotten Tomatoes">
                  <i class="fas fa-lemon"></i> {{ currentCard.ratings.rotten_tomatoes }}%
                </span>
                <span v-if="streaming(currentCard)" class="swipe-meta swipe-streaming"
                      :class="{ mine: currentCard.streaming.on_user_services }"
                      :title="providerNames(currentCard)">
                  <i class="fas fa-tv"></i> {{ streaming(currentCard) }}
                </span>
                <span v-if="genreNames(currentCard)" class="swipe-meta swipe-genres">{{ genreNames(currentCard) }}</span>
              </div>

              <p v-if="currentCard.rationale" class="swipe-rationale">
                <i class="fas fa-robot"></i> {{ currentCard.rationale }}
              </p>
              <p class="swipe-overview" :class="{ expanded: overviewOpen }" @click="overviewOpen = !overviewOpen">
                {{ currentCard.overview || 'No overview available.' }}
              </p>
            </div>
          </article>
        </div>

        <div v-else-if="loading" class="swipe-loading">
          <i class="fas fa-spinner fa-spin"></i>
          <p>Picking cards for you…</p>
        </div>

        <div v-else class="empty-state">
          <i class="fas fa-layer-group"></i>
          <h3>No more cards for now</h3>
          <p v-if="error">{{ error }}</p>
          <button type="button" class="btn btn-primary" @click="loadMore(true)">
            <i class="fas fa-redo"></i> Try again
          </button>
        </div>
      </div>

      <!-- Actions -->
      <div v-if="currentCard" class="swipe-actions">
        <button type="button" class="swipe-action swipe-action-nope" :disabled="busy"
                title="Not for me (←)" aria-label="Not for me" @click="answer('dislike')">
          <i class="fas fa-times"></i>
        </button>
        <button type="button" class="swipe-action swipe-action-seen" :disabled="busy"
                title="Already seen (↓)" aria-label="Already seen" @click="answer('seen')">
          <i class="fas fa-eye"></i>
        </button>
        <button type="button" class="swipe-action swipe-action-like" :disabled="busy"
                title="Like (→)" aria-label="Like" @click="answer('like')">
          <i class="fas fa-heart"></i>
        </button>
      </div>
      <p v-if="currentCard" class="swipe-hint">Swipe or use ← ↓ → on your keyboard</p>
    </template>

    <SwipeProfilePanel
      :open="profileOpen"
      @close="profileOpen = false"
      @reset="onVotesReset"
      @recalibrate="startCalibration"
    />

    <!-- Request? -->
    <teleport to="body">
      <transition name="modal-fade">
        <div v-if="requestCard" class="modal-overlay" @click.self="closeRequest">
          <div class="modal swipe-modal" role="dialog" aria-modal="true" aria-labelledby="swipe-request-title">
            <div class="modal-header">
              <h3 id="swipe-request-title" class="modal-title">
                Request this {{ noun(requestCard) }}?
              </h3>
            </div>
            <div class="modal-body">
              <p><strong>{{ title(requestCard) }}</strong><span v-if="year(requestCard)"> ({{ year(requestCard) }})</span></p>
              <p v-if="streaming(requestCard)" class="swipe-modal-note">
                <i class="fas fa-info-circle"></i>
                Already available {{ streaming(requestCard).replace(/^On /, 'on ') }}<span
                  v-if="requestCard.streaming.on_user_services"> — one of your services</span>.
              </p>
              <p class="swipe-modal-hint">Your like is saved either way.</p>
            </div>
            <div class="modal-footer">
              <button type="button" class="btn btn-secondary" :disabled="requesting" @click="closeRequest">Not now</button>
              <button type="button" class="btn btn-primary" :disabled="requesting" @click="confirmRequest">
                <i :class="requesting ? 'fas fa-spinner fa-spin' : 'fas fa-paper-plane'"></i> Request
              </button>
            </div>
          </div>
        </div>
      </transition>
    </teleport>

    <!-- Already seen: liked it? -->
    <teleport to="body">
      <transition name="modal-fade">
        <div v-if="seenCard" class="modal-overlay" @click.self="seenCard = null">
          <div class="modal swipe-modal" role="dialog" aria-modal="true" aria-labelledby="swipe-seen-title">
            <div class="modal-header">
              <h3 id="swipe-seen-title" class="modal-title">Already seen</h3>
            </div>
            <div class="modal-body">
              <p>Did you like <strong>{{ title(seenCard) }}</strong>?</p>
            </div>
            <div class="modal-footer">
              <button type="button" class="btn btn-ghost" @click="seenCard = null">Cancel</button>
              <button type="button" class="btn btn-secondary" @click="confirmSeen('seen_disliked')">
                <i class="fas fa-thumbs-down"></i> Didn't like it
              </button>
              <button type="button" class="btn btn-primary" @click="confirmSeen('seen_liked')">
                <i class="fas fa-thumbs-up"></i> Liked it
              </button>
            </div>
          </div>
        </div>
      </transition>
    </teleport>
  </div>
</template>

<script>
import { swipeBatch, swipeRequest, swipeStatus, swipeVote } from '@/api/swipeApi.js';
import {
  MAX_POLLS, POLL_INTERVAL_MS, cardKey, cardTitle, cardYear, dragRotation, keyToAction, mediaNoun,
  mergeCards, needsMore, pickLabel, requestMessage, sleep, streamingLabel, swipeDecision,
} from '@/utils/swipeDeck.js';
import SwipeProfilePanel from './SwipeProfilePanel.vue';
import '@/assets/styles/swipePage.css';

const SKIP_CALIBRATION_KEY = 'suggestarr_swipe_skip_calibration';
const MEDIA_TYPE_KEY = 'suggestarr_swipe_media_type';

function readStorage(key, fallback) {
  try {
    return localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}

function writeStorage(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    // Private mode or blocked storage: the preference just is not remembered.
  }
}

export default {
  name: 'SwipePage',

  components: { SwipeProfilePanel },

  data() {
    return {
      status: null,
      deck: [],
      position: 0,
      answered: new Set(),
      loading: false,
      error: '',
      mode: 'auto',
      activeMode: null,
      calibration: { done: 0, target: 15 },
      mediaType: readStorage(MEDIA_TYPE_KEY, 'both'),
      mediaOptions: [
        { value: 'both', label: 'All', icon: 'fas fa-layer-group' },
        { value: 'movie', label: 'Movies', icon: 'fas fa-film' },
        { value: 'tv', label: 'Series', icon: 'fas fa-tv' },
      ],
      moodInput: '',
      mood: '',
      drag: { active: false, startX: 0, dx: 0, pointerId: null },
      leaving: false,
      busy: false,
      overviewOpen: false,
      requestCard: null,
      requesting: false,
      seenCard: null,
      generation: 0,
      profileOpen: false,
      // A calibration run started from the panel counts its own votes.
      forcedCalibration: null,
    };
  },

  computed: {
    currentCard() {
      return this.deck[this.position] || null;
    },
    nextCard() {
      return this.deck[this.position + 1] || null;
    },
    currentKey() {
      return this.currentCard ? cardKey(this.currentCard) : '';
    },
    inCalibration() {
      return this.activeMode === 'calibration';
    },
    calibrationView() {
      return this.forcedCalibration || this.calibration;
    },
    calibrationPercent() {
      const { done, target } = this.calibrationView;
      return target ? Math.min(100, Math.round((done / target) * 100)) : 0;
    },
    cardStyle() {
      if (!this.drag.active && !this.leaving) return {};
      const width = this.$refs.card?.offsetWidth || 400;
      return {
        transform: `translateX(${this.drag.dx}px) rotate(${dragRotation(this.drag.dx, width)}deg)`,
      };
    },
    modalOpen() {
      return Boolean(this.requestCard || this.seenCard || this.profileOpen);
    },
  },

  async mounted() {
    if (readStorage(SKIP_CALIBRATION_KEY, '') === '1') this.mode = 'normal';
    window.addEventListener('keydown', this.onKeydown);
    try {
      const { data } = await swipeStatus();
      this.status = data;
      this.calibration = { done: data.calibration.done, target: data.calibration.target };
    } catch {
      this.error = 'Could not reach the Swipe service.';
      return;
    }
    if (this.status.llm_configured) await this.loadMore(true);
  },

  beforeUnmount() {
    window.removeEventListener('keydown', this.onKeydown);
  },

  methods: {
    title: cardTitle,
    year: cardYear,
    pick(card) {
      return pickLabel(card.pick_type);
    },
    streaming(card) {
      return streamingLabel(card.streaming);
    },
    noun: mediaNoun,
    providerNames(card) {
      return (card.streaming?.providers || []).map(p => p.name).join(', ');
    },
    genreNames(card) {
      return (card.genres || []).slice(0, 3).join(' · ');
    },
    stampOpacity(direction) {
      const width = this.$refs.card?.offsetWidth || 400;
      const ratio = Math.min(1, Math.abs(this.drag.dx) / (width * 0.3));
      if (direction === 'like') return this.drag.dx > 0 ? ratio : 0;
      return this.drag.dx < 0 ? ratio : 0;
    },

    async loadMore(reset = false) {
      if (this.loading) return;
      const generation = reset ? ++this.generation : this.generation;
      if (reset) {
        this.deck = [];
        this.position = 0;
      }
      this.loading = true;
      this.error = '';
      try {
        for (let attempt = 0; attempt < MAX_POLLS; attempt += 1) {
          const { data } = await swipeBatch({ mediaType: this.mediaType, mood: this.mood, mode: this.mode });
          // A filter change while this batch was loading makes it stale.
          if (generation !== this.generation) return;
          this.activeMode = data.mode;
          if (!this.forcedCalibration) this.calibration = data.calibration || this.calibration;
          if (!data.pending) {
            this.deck = mergeCards(this.deck, data.cards, this.answered);
            if (!data.cards.length && this.position >= this.deck.length) {
              this.error = 'The AI found nothing new for these filters.';
            }
            return;
          }
          await sleep(POLL_INTERVAL_MS);
          if (generation !== this.generation) return;
        }
        this.error = 'Cards are taking too long to arrive. Try again in a moment.';
      } catch (error) {
        if (generation !== this.generation) return;
        this.error = error?.response?.data?.message || 'Could not load cards.';
        if (this.deck.length > this.position) this.$toast.error(this.error);
      } finally {
        if (generation === this.generation) this.loading = false;
      }
    },

    maybeLoadMore() {
      if (needsMore(this.deck.length, this.position)) this.loadMore(false);
    },

    setMediaType(value) {
      if (value === this.mediaType) return;
      this.mediaType = value;
      writeStorage(MEDIA_TYPE_KEY, value);
      this.loading = false;
      this.loadMore(true);
    },

    applyMood() {
      const mood = this.moodInput.trim();
      if (mood === this.mood) return;
      this.mood = mood;
      this.loading = false;
      this.loadMore(true);
    },

    clearMood() {
      this.moodInput = '';
      this.applyMood();
    },

    skipCalibration() {
      this.forcedCalibration = null;
      this.mode = 'normal';
      writeStorage(SKIP_CALIBRATION_KEY, '1');
      this.loading = false;
      this.loadMore(true);
    },

    startCalibration() {
      this.forcedCalibration = { done: 0, target: this.calibration.target };
      this.mode = 'calibration';
      this.loading = false;
      this.loadMore(true);
    },

    onVotesReset() {
      this.answered = new Set();
      this.forcedCalibration = null;
      this.calibration = { ...this.calibration, done: 0 };
      this.mode = 'auto';
      writeStorage(SKIP_CALIBRATION_KEY, '0');
      this.loading = false;
      this.loadMore(true);
    },

    // ---- answers ---------------------------------------------------------

    answer(action) {
      const card = this.currentCard;
      if (!card || this.busy || this.modalOpen) return;
      if (action === 'seen') {
        this.seenCard = card;
        return;
      }
      this.fling(action === 'like' ? 1 : -1, () => {
        this.submitVote(card, action);
        if (action === 'like') this.requestCard = card;
      });
    },

    confirmSeen(vote) {
      const card = this.seenCard;
      this.seenCard = null;
      this.fling(vote === 'seen_liked' ? 1 : -1, () => this.submitVote(card, vote));
    },

    fling(direction, then) {
      const width = this.$refs.card?.offsetWidth || 400;
      this.busy = true;
      this.leaving = true;
      this.drag = { ...this.drag, active: false, dx: direction * width * 1.5 };
      window.setTimeout(() => {
        this.leaving = false;
        this.drag = { active: false, startX: 0, dx: 0, pointerId: null };
        this.overviewOpen = false;
        this.position += 1;
        this.busy = false;
        then();
        this.maybeLoadMore();
      }, 220);
    },

    async submitVote(card, vote) {
      this.answered.add(cardKey(card));
      if (this.forcedCalibration) {
        const done = this.forcedCalibration.done + 1;
        if (done >= this.forcedCalibration.target) {
          this.forcedCalibration = null;
          this.mode = readStorage(SKIP_CALIBRATION_KEY, '') === '1' ? 'normal' : 'auto';
        } else {
          this.forcedCalibration = { ...this.forcedCalibration, done };
        }
      } else if (this.inCalibration && this.calibration.done < this.calibration.target) {
        this.calibration = { ...this.calibration, done: this.calibration.done + 1 };
      }
      try {
        await swipeVote(card, vote);
      } catch {
        this.$toast.error(`Could not save your vote on ${cardTitle(card)}.`);
      }
    },

    closeRequest() {
      if (!this.requesting) this.requestCard = null;
    },

    async confirmRequest() {
      const card = this.requestCard;
      this.requesting = true;
      try {
        const { data } = await swipeRequest(card);
        this.$toast.success(requestMessage(data.request_status, cardTitle(card)));
        this.requestCard = null;
      } catch (error) {
        this.$toast.error(error?.response?.data?.message || `Could not request ${cardTitle(card)}.`);
      } finally {
        this.requesting = false;
      }
    },

    // ---- drag & keyboard -------------------------------------------------

    onPointerDown(event) {
      if (this.busy || this.modalOpen || event.button > 0) return;
      this.drag = { active: true, startX: event.clientX, dx: 0, pointerId: event.pointerId };
      event.currentTarget.setPointerCapture?.(event.pointerId);
    },

    onPointerMove(event) {
      if (!this.drag.active || event.pointerId !== this.drag.pointerId) return;
      this.drag = { ...this.drag, dx: event.clientX - this.drag.startX };
    },

    onPointerUp(event) {
      if (!this.drag.active || event.pointerId !== this.drag.pointerId) return;
      const width = this.$refs.card?.offsetWidth || 400;
      const decision = swipeDecision(this.drag.dx, width);
      if (decision) {
        this.answer(decision);
      } else {
        this.drag = { active: false, startX: 0, dx: 0, pointerId: null };
      }
    },

    onPointerCancel() {
      this.drag = { active: false, startX: 0, dx: 0, pointerId: null };
    },

    onKeydown(event) {
      const target = event.target;
      if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable)) return;
      if (event.key === 'Escape' && this.modalOpen) {
        this.seenCard = null;
        this.closeRequest();
        return;
      }
      const action = keyToAction(event.key);
      if (!action || !this.currentCard || this.modalOpen) return;
      event.preventDefault();
      this.answer(action);
    },
  },
};
</script>
