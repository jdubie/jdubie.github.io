(defproject snake "0.1.0"
  :dependencies [[org.clojure/clojure "1.7.0"]
                 [org.clojure/clojurescript "1.7.122"]
                 [com.cemerick/piggieback "0.2.1"]
                 [org.clojure/tools.nrepl "0.2.10"]
                 [figwheel-sidecar "0.5.0"]]
  :plugins [[lein-cljsbuild "1.1.0"]]
  :repl-options {:nrepl-middleware [cemerick.piggieback/wrap-cljs-repl]}
  :cljsbuild {:builds [{:id           "snake"
                        :source-paths ["src"]
                        :figwheel     true
                        :compiler     {:main       "snake.core"
                                       :asset-path "js/snake.core.out"
                                       :output-to  "resources/public/js/snake.core.js"
                                       :output-dir "resources/public/js/snake.core.out"}}]})
