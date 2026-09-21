import axios from 'axios';

// Swipe: AI-picked cards shown one at a time. Every call acts on the signed-in account.

export const swipeStatus = () => axios.get('/api/swipe/status');

export const swipeBatch = ({ mediaType = 'both', mood = '', mode = 'auto' } = {}) =>
    axios.get('/api/swipe/batch', { params: { media_type: mediaType, mood: mood || undefined, mode } });

export const swipeVote = (card, vote) => axios.post('/api/swipe/vote', { card, vote });

export const swipeRequest = (card) => axios.post('/api/swipe/request', { card });

export const swipeProfile = () => axios.get('/api/swipe/profile');

export const swipeProfileSave = (profileText) =>
    axios.put('/api/swipe/profile', { profile_text: profileText });

export const swipeProfileRefresh = () => axios.post('/api/swipe/profile/refresh');

export const swipeStats = () => axios.get('/api/swipe/stats');

export const swipeResetVotes = () => axios.delete('/api/swipe/votes');
