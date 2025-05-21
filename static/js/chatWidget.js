const ChatWidget = {
    config: {
        iframeSrc: '/',
        fabIconHtml: '<i class="fas fa-comment-dots"></i>',
        iframeWidth: '350px',
        iframeZIndex: 1001,
        fabZIndex: 1000,
        rootPath: '', // For FastAPI root_path, to be set via init
        pushElementSelector: null,
        pushElementMarginTransition: 'margin-right 0.5s ease-in-out'
    },
    elements: {
        fab: null,
        iframeContainer: null,
        iframe: null,
        pushedElement: null
    },
    styleId: 'chat-widget-styles',
    sessionId: null,

    init: function(options) {
        this.sessionId = localStorage.getItem('chat_session_id');
        if (!this.sessionId) {
            if (window.crypto && crypto.randomUUID) {
                this.sessionId = crypto.randomUUID();
                console.log('ChatWidget: New session ID generated:', this.sessionId);
            } else {
                this.sessionId = 'fallback-session-' + Date.now() + '-' + Math.random().toString(36).substring(2, 15);
                console.warn('ChatWidget: crypto.randomUUID() not available, using fallback session ID.');
            }
            localStorage.setItem('chat_session_id', this.sessionId);
        } else {
            console.log('ChatWidget: Using existing session ID:', this.sessionId);
        }
        
        if (options) {
            this.config = { ...this.config, ...options };
        }
        this.config.iframeSrc = this.config.rootPath ? this.config.rootPath + '/' : '/';

        this.injectStyles();
        this.createFab();
        this.createIframeContainer();
        
        if (this.config.pushElementSelector) {
            this.elements.pushedElement = document.querySelector(this.config.pushElementSelector);
            if (this.elements.pushedElement && this.config.pushElementMarginTransition) {
                this.elements.pushedElement.style.transition = this.config.pushElementMarginTransition;
            } else if (!this.elements.pushedElement) {
                console.warn(`ChatWidget: Element to push with selector '${this.config.pushElementSelector}' not found.`);
            }
        }
        
        window.hideChatWidget = this.hideChat.bind(this);
        console.log('ChatWidget initialized. Session ID:', this.sessionId);
    },

    injectStyles: function() {
        if (document.getElementById(this.styleId)) return;

        const styles = `
            .chat-widget-fab {
                position: fixed;
                bottom: 30px;
                right: 30px;
                width: 60px;
                height: 60px;
                background-color: #1a73e8; /* Google Blue */
                color: white;
                border-radius: 50%;
                display: flex;
                justify-content: center;
                align-items: center;
                font-size: 24px;
                cursor: pointer;
                box-shadow: 0 4px 8px rgba(0, 0, 0, 0.2);
                transition: transform 0.3s ease, opacity 0.3s ease;
                z-index: ${this.config.fabZIndex};
            }
            .chat-widget-fab:hover {
                transform: scale(1.1);
            }
            .chat-widget-fab.hidden {
                transform: scale(0);
                opacity: 0;
            }
            .chat-widget-iframe-container {
                position: fixed;
                top: 0;
                right: 0;
                width: ${this.config.iframeWidth};
                height: 100%;
                transform: translateX(100%);
                transition: transform 0.5s ease-in-out;
                background-color: #fff;
                z-index: ${this.config.iframeZIndex};
                overflow: hidden;
                box-shadow: -2px 0 5px rgba(0,0,0,0.1);
            }
            .chat-widget-iframe-container.visible {
                transform: translateX(0);
            }
            .chat-widget-iframe-container iframe {
                width: 100%;
                height: 100%;
                border: none;
            }
        `;
        const styleSheet = document.createElement('style');
        styleSheet.id = this.styleId;
        styleSheet.type = 'text/css';
        styleSheet.innerText = styles.replace(/\/\* ... 기존 스타일 ... \*\//g, '{/* 실제 스타일 내용 */}').replace('{/* 실제 스타일 내용 */}', 
            `position: fixed; bottom: 30px; right: 30px; width: 60px; height: 60px; background-color: #1a73e8; color: white; border-radius: 50%; display: flex; justify-content: center; align-items: center; font-size: 24px; cursor: pointer; box-shadow: 0 4px 8px rgba(0, 0, 0, 0.2); transition: transform 0.3s ease, opacity 0.3s ease; z-index: ${this.config.fabZIndex};`
        ).replace('{/* 실제 스타일 내용 */}', 
            `transform: scale(1.1);`
        ).replace('{/* 실제 스타일 내용 */}', 
            `transform: scale(0); opacity: 0;`
        ).replace('{/* 실제 스타일 내용 */}', 
            `position: fixed; top: 0; right: 0; width: ${this.config.iframeWidth}; height: 100%; transform: translateX(100%); transition: transform 0.5s ease-in-out; background-color: #fff; z-index: ${this.config.iframeZIndex}; overflow: hidden; box-shadow: -2px 0 5px rgba(0,0,0,0.1);`
        ).replace('{/* 실제 스타일 내용 */}', 
            `transform: translateX(0);`
        ).replace('{/* 실제 스타일 내용 */}', 
            `width: 100%; height: 100%; border: none;`
        );
        document.head.appendChild(styleSheet);
    },

    createFab: function() {
        this.elements.fab = document.createElement('div');
        this.elements.fab.className = 'chat-widget-fab';
        this.elements.fab.innerHTML = this.config.fabIconHtml;
        this.elements.fab.addEventListener('click', this.showChat.bind(this));
        document.body.appendChild(this.elements.fab);
    },

    createIframeContainer: function() {
        this.elements.iframeContainer = document.createElement('div');
        this.elements.iframeContainer.className = 'chat-widget-iframe-container';
        
        this.elements.iframe = document.createElement('iframe');
        this.elements.iframe.title = 'Chat Application';
        this.elements.iframe.allow = 'display-capture'; 
        this.elements.iframeContainer.appendChild(this.elements.iframe);
        document.body.appendChild(this.elements.iframeContainer);
    },

    showChat: function() {
        const iframeTargetSrc = this.config.iframeSrc + '?sessionId=' + encodeURIComponent(this.sessionId);
        
        if (this.elements.iframe.getAttribute('src') !== iframeTargetSrc) {
            console.log('ChatWidget: Setting iframe src to:', iframeTargetSrc);
            this.elements.iframe.src = iframeTargetSrc;
        }
        
        if (this.elements.pushedElement) {
            this.elements.pushedElement.style.marginRight = this.config.iframeWidth;
        }
        
        this.elements.iframeContainer.classList.add('visible');
        this.elements.fab.classList.add('hidden');
    },

    hideChat: function() {
        if (this.elements.pushedElement) {
            this.elements.pushedElement.style.marginRight = '0px';
        }
        this.elements.iframeContainer.classList.remove('visible');
        this.elements.fab.classList.remove('hidden');
    }
};

(function() {
    let faLoaded = false;
    const links = document.getElementsByTagName('link');
    for (let i = 0; i < links.length; i++) {
        if (links[i].href && links[i].href.includes('font-awesome')) {
            faLoaded = true;
            break;
        }
    }
    if (!faLoaded) {
        const faLink = document.createElement('link');
        faLink.rel = 'stylesheet';
        faLink.href = 'https://cdnjs.cloudflare.com/ajax/libs/font-awesome/5.15.4/css/all.min.css'; // Or 6.x.x if preferred
        document.head.appendChild(faLink);
        console.log('ChatWidget: FontAwesome CSS loaded dynamically.');
    }
})(); 