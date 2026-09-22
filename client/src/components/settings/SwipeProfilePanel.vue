<template>
  <teleport to="body">
    <transition name="modal-fade">
      <div v-if="open" class="modal-overlay" @click.self="close">
        <div class="modal swipe-profile-modal" role="dialog" aria-modal="true" aria-labelledby="swipe-profile-title">
          <div class="modal-header">
            <h3 id="swipe-profile-title" class="modal-title"><i class="fas fa-user-astronaut"></i> My taste</h3>
            <button type="button" class="modal-close" aria-label="Close" @click="close">
              <i class="fas fa-times"></i>
            </button>
          </div>

          <div class="modal-body">
            <div v-if="loading" class="swipe-profile-loading">
              <i class="fas fa-spinner fa-spin"></i> Loading…
            </div>

            <template v-else>
              <section class="swipe-profile-section">
                <div class="swipe-profile-section-head">
                  <h4>Taste profile</h4>
                  <span v-if="origin" class="swipe-profile-origin" :class="{ mine: profile && profile.user_edited }">
                    {{ origin }}
                  </span>
                </div>
                <p v-if="!profile && !draft" class="swipe-profile-empty">
                  No profile yet. It is written after calibration, or generate it now from your viewing history.
                </p>
                <textarea
                  v-model="draft"
                  class="swipe-profile-text"
                  rows="9"
                  :maxlength="maxLength"
                  placeholder="Describe what you love and what you avoid. The AI will keep your corrections."
                  aria-label="Taste profile"
                ></textarea>
                <div class="swipe-profile-row">
                  <small v-if="profile && profile.votes_since_update">
                    {{ profile.votes_since_update }} vote(s) since the last update — refreshed every 10 votes.
                  </small>
                  <span class="swipe-profile-spacer"></span>
                  <button type="button" class="btn btn-secondary btn-sm" :disabled="busy" @click="regenerate">
                    <i :class="regenerating ? 'fas fa-spinner fa-spin' : 'fas fa-magic'"></i> Regenerate
                  </button>
                  <button type="button" class="btn btn-primary btn-sm" :disabled="busy || !dirty" @click="save">
                    <i :class="saving ? 'fas fa-spinner fa-spin' : 'fas fa-save'"></i> Save
                  </button>
                </div>
              </section>

              <section class="swipe-profile-section">
                <h4>Stats</h4>
                <p v-if="!rows.length" class="swipe-profile-empty">Answer a few cards to see your stats.</p>
                <dl v-else class="swipe-profile-stats">
                  <template v-for="row in rows" :key="row.label">
                    <dt>{{ row.label }}</dt>
                    <dd>{{ row.value }}</dd>
                  </template>
                </dl>
              </section>

              <section class="swipe-profile-section">
                <h4>Preferences</h4>
                <label class="swipe-profile-toggle">
                  <input type="checkbox" :checked="autoRequest"
                         @change="$emit('update:autoRequest', $event.target.checked)" />
                  <span>
                    Request liked cards right away
                    <small>Skips the "Request this?" question. Approval rules still apply.</small>
                  </span>
                </label>
              </section>

              <section class="swipe-profile-section">
                <h4>Start over</h4>
                <div class="swipe-profile-row">
                  <button type="button" class="btn btn-secondary btn-sm" :disabled="busy" @click="recalibrate">
                    <i class="fas fa-compass"></i> Run calibration again
                  </button>
                  <button v-if="!confirmReset" type="button" class="btn btn-outline btn-sm swipe-profile-danger"
                          :disabled="busy || !stats || !stats.total" @click="confirmReset = true">
                    <i class="fas fa-trash"></i> Reset my votes
                  </button>
                </div>
                <div v-if="confirmReset" class="swipe-profile-confirm" role="alert">
                  <p>Delete your {{ stats.total }} vote(s)? Cards you answered can come back. Your profile text is kept.</p>
                  <div class="swipe-profile-row">
                    <button type="button" class="btn btn-ghost btn-sm" @click="confirmReset = false">Cancel</button>
                    <button type="button" class="btn btn-danger btn-sm" :disabled="busy" @click="reset">
                      <i :class="resetting ? 'fas fa-spinner fa-spin' : 'fas fa-trash'"></i> Delete votes
                    </button>
                  </div>
                </div>
              </section>
            </template>
          </div>
        </div>
      </div>
    </transition>
  </teleport>
</template>

<script>
import {
  swipeProfile, swipeProfileRefresh, swipeProfileSave, swipeResetVotes, swipeStats,
} from '@/api/swipeApi.js';
import { MAX_POLLS, POLL_INTERVAL_MS, sleep } from '@/utils/swipeDeck.js';
import { PROFILE_MAX_LENGTH, isProfileDirty, profileOrigin, statsRows } from '@/utils/swipeProfile.js';

export default {
  name: 'SwipeProfilePanel',

  props: {
    open: { type: Boolean, default: false },
    autoRequest: { type: Boolean, default: false },
  },

  emits: ['close', 'reset', 'recalibrate', 'update:autoRequest'],

  data() {
    return {
      loading: false,
      profile: null,
      stats: null,
      draft: '',
      saving: false,
      regenerating: false,
      resetting: false,
      confirmReset: false,
      maxLength: PROFILE_MAX_LENGTH,
    };
  },

  computed: {
    rows() {
      return statsRows(this.stats);
    },
    origin() {
      return profileOrigin(this.profile);
    },
    dirty() {
      return isProfileDirty(this.draft, this.profile?.profile_text);
    },
    busy() {
      return this.saving || this.regenerating || this.resetting;
    },
  },

  watch: {
    open(value) {
      if (value) this.load();
      else this.confirmReset = false;
    },
  },

  methods: {
    async load() {
      this.loading = true;
      try {
        const [profile, stats] = await Promise.all([swipeProfile(), swipeStats()]);
        if (!this.dirty) this.setProfile(profile.data.profile);
        else this.profile = profile.data.profile;
        this.stats = stats.data.stats;
      } catch {
        this.$toast.error('Could not load your taste profile.');
      } finally {
        this.loading = false;
      }
    },

    setProfile(profile) {
      this.profile = profile;
      this.draft = profile?.profile_text || '';
    },

    close() {
      if (this.dirty && !window.confirm('Discard your unsaved changes to the profile?')) return;
      this.$emit('close');
    },

    async save() {
      this.saving = true;
      try {
        const { data } = await swipeProfileSave(this.draft.trim());
        this.setProfile(data.profile);
        this.$toast.success('Profile saved. The AI will keep your corrections.');
      } catch (error) {
        this.$toast.error(error?.response?.data?.message || 'Could not save the profile.');
      } finally {
        this.saving = false;
      }
    },

    async regenerate() {
      if (this.dirty && !window.confirm('Regenerating replaces your unsaved changes. Continue?')) return;
      this.regenerating = true;
      const before = this.profile?.updated_at || null;
      try {
        await swipeProfileRefresh();
        for (let attempt = 0; attempt < MAX_POLLS; attempt += 1) {
          await sleep(POLL_INTERVAL_MS);
          const { data } = await swipeProfile();
          if (data.refreshing) continue;
          if (data.refresh_error) {
            this.$toast.error('The AI could not rewrite your profile. Try again.');
          } else if (data.profile && data.profile.updated_at !== before) {
            this.setProfile(data.profile);
            this.$toast.success('Profile updated from your votes and viewing history.');
          } else {
            this.$toast.open({ message: 'Not enough votes or history yet to write a profile.', type: 'info' });
          }
          return;
        }
        this.$toast.error('The profile is taking too long. Check again in a moment.');
      } catch (error) {
        this.$toast.error(error?.response?.data?.message || 'Could not regenerate the profile.');
      } finally {
        this.regenerating = false;
      }
    },

    recalibrate() {
      this.$emit('recalibrate');
      this.$emit('close');
    },

    async reset() {
      this.resetting = true;
      try {
        const { data } = await swipeResetVotes();
        this.confirmReset = false;
        this.$toast.success(`${data.deleted} vote(s) deleted.`);
        this.$emit('reset');
        await this.load();
      } catch (error) {
        this.$toast.error(error?.response?.data?.message || 'Could not reset your votes.');
      } finally {
        this.resetting = false;
      }
    },
  },
};
</script>
