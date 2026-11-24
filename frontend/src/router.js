import { createRouter, createWebHistory } from 'vue-router'
import Landing from './views/Landing.vue'

// Only the landing page is in the main bundle. /admin in particular must stay lazy: the
// organizer app pulls in the schedule editors and the ZXing scanner, none of which a
// volunteer reading the landing page should download on conference wifi.
const routes = [
  { path: '/', component: Landing },
  {
    path: '/apply',
    component: () => import('./views/apply/ApplyShell.vue'),
    children: [
      { path: '',       component: () => import('./views/apply/ApplyHome.vue') },
      { path: 'signup', component: () => import('./views/apply/ApplySignup.vue') },
      { path: 'login',  component: () => import('./views/apply/ApplyLogin.vue') },
      { path: 'inbox',  component: () => import('./views/apply/ApplyInbox.vue') },
      // The magic link lands here. The GET is inert; only the button POSTs the token.
      { path: 'verify', component: () => import('./views/apply/ApplyVerify.vue') },
      // signedIn: the header shows Sign out (PortalLayout). ApplyMe sends anyone without a
      // session to the sign-in page.
      { path: 'me',     component: () => import('./views/apply/ApplyMe.vue'), meta: { signedIn: true } },
    ],
  },
  // /admin/audit is the Audit Log tab: security alert emails link there. An alias, not a second
  // route, so switching tabs doesn't remount the whole organizer app.
  { path: '/admin', alias: '/admin/audit', component: () => import('./views/Admin.vue') },
  // Emailed links. Both are inert on load (a GET check only); the button POSTs the token.
  { path: '/admin/invite', component: () => import('./views/AcceptInvite.vue') },
  { path: '/admin/reset',  component: () => import('./views/ResetPassword.vue') },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior: () => ({ top: 0 }),
})

// / and /apply/* wear the conference site's light theme (assets/portal-theme.css); /admin*
// stays Noir dark. On <html>, not a wrapper, so PrimeVue overlays teleported to <body> follow.
// index.html sets the same classes before first paint.
router.afterEach((to) => {
  const admin = to.path.startsWith('/admin')
  document.documentElement.classList.toggle('dark-app', admin)
  document.documentElement.classList.toggle('bsc-portal', !admin)
})

export default router
