import js from "@eslint/js";
import globals from "globals";

export default [
  js.configs.recommended,
  {
    files: ["app/static/**/*.js"],
    languageOptions: {
      ecmaVersion: "latest",
      // classic <script> tags: top-level names are shared between the files of a page
      sourceType: "script",
      globals: globals.browser,
    },
  },
  {
    // auth.js runs first and defines these for app.js
    files: ["app/static/app.js"],
    languageOptions: {
      globals: { AUTH_KEY: "readonly", getAuth: "readonly", toLogin: "readonly" },
    },
  },
];
