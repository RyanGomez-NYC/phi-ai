/*
 * OHIF Viewer configuration for the PHI AI Platform.
 *
 * THIS FILE IS THE ENTIRE INTEGRATION. The viewer is the official,
 * unmodified OHIF distribution - see docker-compose.yml's `viewer`
 * service, which runs the published `ohif/app` image at a pinned tag.
 * Nothing in this repository forks, patches or vendors OHIF source, and
 * that is deliberate: updating the viewer must be a version bump, not a
 * merge. To upgrade, change the image tag in docker-compose.yml, re-read
 * the OHIF release notes for changes to the keys below, and restart.
 * There is no build step here and no OHIF code to re-apply patches to.
 *
 * The container reads this at /usr/share/nginx/html/app-config.js. The
 * schema is OHIF's, not this project's - see
 * https://docs.ohif.org/configuration/configurationFiles. If a key below
 * stops being honoured, that is an OHIF change and the fix is here, not
 * in the platform.
 *
 * WHY THE VIEWER IS ON ITS OWN ORIGIN. The platform's own interface runs
 * under `script-src 'none'` (core/web/security.py) - it ships no
 * JavaScript at all, so that no cross-site scripting bug on a page
 * displaying PHI can execute anything. A medical image viewer cannot run
 * that way: OHIF needs scripts, web workers and WASM codecs. Serving it
 * from a separate origin keeps that guarantee intact for every page of
 * the platform, and confines the scripting the viewer genuinely requires
 * to an origin that holds no session and renders no record page. The
 * cost is CORS, which core/web/app.py handles with an exact-origin
 * allowlist scoped to /dicomweb.
 */

window.config = {
  routerBasename: '/',
  extensions: [],
  modes: [],

  /*
   * The worklist. Left ON so a records clerk can find a study, but note
   * that reaching it still requires a purpose of use recorded in the
   * platform's session - the DICOMweb API refuses every request without
   * one (core/web/dicomweb_routes.py). Set to false to force every entry
   * to come through the platform's own patient record page.
   */
  showStudyList: true,

  /*
   * Web workers decode pixel data off the main thread. More is faster and
   * uses more memory; 3 is OHIF's own default and is sane for a records
   * workstation rather than a radiology reading station.
   */
  maxNumberOfWebWorkers: 3,

  defaultDataSourceName: 'dicomweb',

  dataSources: [
    {
      namespace: '@ohif/extension-default.dataSourcesModule.dicomweb',
      sourceName: 'dicomweb',
      configuration: {
        /*
         * friendlyName is rendered in OHIF's own UI as the name of the
         * source the study came from, so it is a user-visible product
         * name and says the product's name. `name` is OHIF's internal
         * handle for this source; nothing dereferences it by value here
         * (defaultDataSourceName and sourceName are both 'dicomweb'), so
         * renaming it moves nothing.
         */
        friendlyName: 'PHI AI Platform',
        name: 'phiai',

        /*
         * All three point at the platform's own DICOMweb API. Replace the
         * host with the platform's externally reachable URL - the one the
         * BROWSER can reach, not a container name, because these URLs are
         * fetched by the user's browser and not by the viewer container.
         *
         * This must match PHI_AI_IMAGING_VIEWER_ORIGIN's counterpart:
         * the platform allows CORS from the viewer's origin, and the
         * viewer calls the platform's origin. Getting one of the two wrong
         * shows up as a CORS error in the browser console and an empty
         * study list, not as a server error.
         *
         * The hostnames below are placeholders every operator overwrites
         * with their own deployment's URL.
         */
        wadoUriRoot: 'https://phi-ai.example.org/dicomweb',
        qidoRoot: 'https://phi-ai.example.org/dicomweb',
        wadoRoot: 'https://phi-ai.example.org/dicomweb',

        /*
         * Send cookies with every DICOMweb request. Required: the platform
         * authenticates through the same session cookie the rest of the
         * interface uses, and a cross-origin fetch drops cookies unless
         * credentials are requested explicitly. Without this every
         * request arrives unauthenticated and is refused.
         */
        requestOptions: {
          requestCredentials: 'include',
        },

        /*
         * Retrieval strategy. `wadors` fetches pixel frames through
         * WADO-RS, which is what core/web/dicomweb_routes.py's frames
         * endpoint serves - in whatever transfer syntax the study was
         * stored in. The platform never transcodes, so the decoding
         * happens here, in the browser, which is also the only place that
         * would not silently alter data the platform exists to preserve.
         */
        imageRendering: 'wadors',
        thumbnailRendering: 'wadors',

        /*
         * Load series on demand rather than pulling a whole study up
         * front. Important here specifically: the platform decrypts each
         * instance to serve its metadata (see
         * core/dicom/dicomweb.py's header), so eager loading a 2,000-slice
         * CT is 2,000 decryptions before the first image appears.
         */
        enableStudyLazyLoad: true,

        /*
         * What the platform's QIDO implementation actually supports - see
         * core/dicom/index.py. Claiming support it does not have makes
         * the viewer send queries that silently return nothing.
         *
         *   supportsWildcard      - `*` and `?` matching: implemented
         *   supportsFuzzyMatching - phonetic name matching: NOT implemented
         *   qidoSupportsIncludeField - arbitrary includefield: NOT implemented
         *   staticWado            - this is a live server, not a file dump
         */
        supportsWildcard: true,
        supportsFuzzyMatching: false,
        qidoSupportsIncludeField: false,
        staticWado: false,

        /*
         * The platform returns frames as multipart/related, per PS3.18
         * §10.4, so nothing is declared single-part. bulkDataURI is off
         * because the platform inlines no bulk data references in its
         * metadata responses - pixel data is excluded from metadata and
         * fetched through the frames endpoint instead.
         */
        singlepart: false,
        bulkDataURI: {
          enabled: false,
        },
        omitQuotationForMultipartRequest: true,
      },
    },
  ],
};
