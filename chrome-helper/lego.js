(() => {
  if (location.origin !== 'https://www.lego.com' || !location.pathname.startsWith('/en-us/') || window !== window.top) return;
  const fields = `carts { ... on ElementCart { id type shippingMethod { isFree } } ... on BrickCart { brickLineItems { id sku quantity } } }`;
  const main = `id lineItems { id quantity customisationId productVariant { sku } }`;
  const queries = {
    readCarts: `query ElementCartQuery($cartTypes:[CartType!]!){ elementCarts(types:$cartTypes){ ${fields} } }`,
    readMain: `query RefreshUserCartQuery { me { ... on LegoUser { cart { ${main} } } } }`,
    catalogue: `query PickABrickQuery($input:ElementQueryInput!){searchElements(input:$input){ results { id availability deliveryChannel maxOrderQuantity price {centAmount currencyCode} } }}`,
    add: `mutation ElementCartsAddToCart($items:[ElementInput!]!,$cartType:CartType!,$returnCarts:[CartType!]!){elementCartsAddToCart(input:{items:$items,cartType:$cartType,returnCarts:$returnCarts}){${fields}}}`,
    transfer: `mutation AddElementCartToMainCart($elementCartId:String!,$elementQuantity:Int!,$hasHandlingFee:Boolean){addElementCartToMainCart(input:{cartId:$elementCartId,elementQuantity:$elementQuantity,hasHandlingFee:$hasHandlingFee}){${main}}}`,
  };
  chrome.runtime.onMessage.addListener((message, sender, respond) => {
    if (sender.id !== chrome.runtime.id || message.type !== 'lego-operation' || !Object.hasOwn(queries, message.operation)) return;
    // Never edit an existing custom group; this helper only adds reviewed quantities.
    if (['add', 'transfer'].includes(message.operation)) {
      try {
        const groups=JSON.parse(localStorage.getItem('element-groups') || '[]');
        if (!Array.isArray(groups) || groups.length) throw Error('Finish editing the current LEGO parts group before using the helper.');
      } catch {
        respond({ok:false,error:'Review the current LEGO parts group before using the helper.'}); return;
      }
    }
    fetch('/api/graphql', { method: 'POST', credentials: 'include', headers: {'Content-Type':'application/json','x-locale':'en-US'},
      body: JSON.stringify({ query: queries[message.operation], variables: message.variables }) })
      .then(async response => { if (!response.ok) throw Error(`LEGO returned HTTP ${response.status}. Open its tab to continue.`);
        const body = await response.json(); if (body.errors?.length || !body.data) throw Error('LEGO rejected this operation. Review its tab.'); return body.data; })
      .then(body => respond({ok:true,body}), error => respond({ok:false,error:error.message}));
    return true;
  });
})();
