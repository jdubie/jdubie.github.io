(require '[figwheel-sidecar.system :as system]
         '[com.stuartsierra.component :as component])

(def running-system
  (component/start
   (system/figwheel-system
     (system/fetch-config))))

(comment
  (system/cljs-repl running-system))
